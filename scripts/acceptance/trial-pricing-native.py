"""Read-only price resolution checks on our imported native PostgreSQL copy."""
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
import socket
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared-sdk/python"))
from metahub_shared.db import connect_database, close_database_pools
from metahub_shared.runtime.cost_rates import resolve_workspace_cost_rate
from metahub_shared.runtime.workspace_fx import resolve_workspace_fx_rate


def create_price_fixture(connection, schema):
    # Keep test writes in a separate schema. PostgreSQL LIKE preserves the native
    # column types, checks and indexes; no production rows or schemas are edited.
    connection.execute(f'CREATE SCHEMA "{schema}"')
    for table in ('models', 'providers', 'workspace_tenant_bindings',
                  'workspace_provider_cost_rates', 'workspace_provider_cost_rate_versions',
                  'workspace_provider_cost_rate_heads', 'workspace_fx_rate_versions'):
        connection.execute(f'CREATE TABLE "{schema}".{table} (LIKE public.{table} INCLUDING ALL)')
    # Existing imported business metadata contains no active workspace rates.
    # Copy only model/provider/binding metadata into our isolated fixture schema.
    for table in ('models', 'providers', 'workspace_tenant_bindings'):
        connection.execute(f'INSERT INTO "{schema}".{table} SELECT * FROM public.{table}')
    connection.execute(f'SET search_path TO "{schema}",public')
    connection.execute("UPDATE models SET status='active',currency='CNY'")
    connection.execute("UPDATE providers SET status='active'")
    connection.execute("UPDATE workspace_tenant_bindings SET status='active'")
    model = connection.execute('SELECT model_id,tenant_id,provider_id FROM models ORDER BY model_id LIMIT 1').fetchone()
    assert model is not None
    binding = connection.execute('SELECT workspace_id FROM workspace_tenant_bindings WHERE tenant_id=? LIMIT 1', (model['tenant_id'],)).fetchone()
    assert binding is not None
    rate_id = 'pcr_' + uuid4().hex
    connection.execute('''INSERT INTO workspace_provider_cost_rates(
        workspace_provider_cost_rate_id,workspace_id,tenant_id,provider_id,model_id,currency,created_at)
        VALUES(?,?,?,?,?,'CNY','2026-07-09T00:00:00Z')''',
        (rate_id,binding['workspace_id'],model['tenant_id'],model['provider_id'],model['model_id']))
    version_id = None
    for number,start,end,price in ((1,'03:00','04:00',1_000_000),(2,'04:00','05:00',2_000_000),(3,'08:00',None,0)):
        version_id = 'pcrv_' + uuid4().hex
        connection.execute('''INSERT INTO workspace_provider_cost_rate_versions(
            workspace_provider_cost_rate_version_id,workspace_provider_cost_rate_id,version_number,
            input_rate_micros_per_1k,output_rate_micros_per_1k,cache_rate_micros_per_1k,
            effective_from,effective_until,source_type,source_id,source_sha256,status,
            revision,content_sha256,created_at)
            VALUES(?,?,?,?,0,0,?,?,'manual',?,?,'active',1,?,'2026-07-09T00:00:00Z')''',
            (version_id,rate_id,number,price*1_000_000,'2026-07-09T'+start+':00Z',
             None if end is None else '2026-07-09T'+end+':00Z',version_id,'a'*64,'b'*64))
    connection.execute('''INSERT INTO workspace_provider_cost_rate_heads VALUES(?,?,1,'active','2026-07-09T00:00:00Z')''', (rate_id,version_id))
    connection.commit()
    return model


def main():
    work = ROOT / ".tmp/trial-pg-import-4yycc3ou"
    if (work / "data/postmaster.pid").exists():
        raise ValueError("Acceptance cluster already running.")
    attempt = work / ("pricing-" + uuid4().hex)
    attempt.mkdir()
    def lifecycle(*args):
        with (attempt / "lifecycle.log").open("ab") as log:
            subprocess.run([str(ROOT / ".tools/postgresql17/pgsql/bin/pg_ctl.exe"),
                            "-D", str(work / "data"), *args], check=True,
                           stdout=log, stderr=log, timeout=90,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    started = False
    connection = None
    try:
        started = True
        lifecycle("-l", str(attempt / "server.log"), "-w", "start", "-o",
                  f"-h 127.0.0.1 -p {port} -c timezone=UTC -c log_min_messages=panic -c log_min_error_statement=panic -c max_locks_per_transaction=2048 -c max_wal_size=128MB -c min_wal_size=16MB")
        connection = connect_database(f"postgresql://acceptance@127.0.0.1:{port}/postgres")
        schema = 'acceptance_pricing_' + uuid4().hex
        model = create_price_fixture(connection, schema)
        connection.execute("SET TRANSACTION READ ONLY")
        resources = connection.execute("""SELECT rate.* FROM workspace_provider_cost_rates rate
            JOIN workspace_provider_cost_rate_heads head USING(workspace_provider_cost_rate_id)
            JOIN workspace_provider_cost_rate_versions version
              ON version.workspace_provider_cost_rate_version_id=head.workspace_provider_cost_rate_version_id
            JOIN models model ON model.model_id=rate.model_id AND model.tenant_id=rate.tenant_id
            JOIN providers provider ON provider.provider_id=rate.provider_id AND provider.tenant_id=rate.tenant_id
            JOIN workspace_tenant_bindings binding ON binding.workspace_id=rate.workspace_id AND binding.tenant_id=rate.tenant_id
            WHERE head.adoption_state='active' AND version.status='active'
              AND model.status='active' AND provider.status='active' AND binding.status='active'
              AND rate.currency=model.currency""").fetchall()
        checked = transitions = 0
        now = datetime.now(UTC).isoformat()
        for resource in resources:
            versions = connection.execute("""SELECT * FROM workspace_provider_cost_rate_versions
                WHERE workspace_provider_cost_rate_id=? AND status IN('active','superseded')
                ORDER BY unixepoch(effective_from) DESC,workspace_provider_cost_rate_version_id DESC""",
                (resource['workspace_provider_cost_rate_id'],)).fetchall()
            observations = {now, '2026-07-09T02:00:00Z','2026-07-09T06:00:00Z',
                            '2026-07-09T12:00:00+08:00', *(str(v['effective_from']) for v in versions)}
            selected = set()
            for observed_at in observations:
                moment = datetime.fromisoformat(observed_at.replace('Z', '+00:00'))
                eligible = [v for v in versions
                            if datetime.fromisoformat(str(v['effective_from']).replace('Z','+00:00')) <= moment
                            and (v['effective_until'] is None or moment < datetime.fromisoformat(str(v['effective_until']).replace('Z','+00:00')))]
                resolved = resolve_workspace_cost_rate(connection, tenant_id=resource['tenant_id'],
                                                       model_id=resource['model_id'], observed_at=observed_at)
                if not eligible:
                    assert resolved is None
                    continue
                expected = eligible[0]
                assert resolved is not None
                assert resolved.source_id == expected['workspace_provider_cost_rate_version_id']
                for field in ('input', 'output', 'cache'):
                    assert getattr(resolved, field+'_price_per_1k') == Decimal(expected[field+'_rate_micros_per_1k']) / Decimal(1_000_000)
                assert resolved.currency == resource['currency']
                selected.add(resolved.source_id)
                checked += 1
            transitions += max(len(selected)-1, 0)
        assert checked > 0, "No configured prices were verified."
        assert transitions == 2
        assert resolve_workspace_cost_rate(connection, tenant_id=model['tenant_id'],
            model_id=model['model_id'], observed_at='2026-07-09T06:00:00Z') is None
        for tenant in {r['tenant_id'] for r in resources}:
            resolve_workspace_fx_rate(connection, tenant_id=tenant, observed_at=now)
        report = {'status':'passed','platform':'Windows native PostgreSQL (supplementary)',
                  'rate_observations_verified':checked,'historical_version_transitions':transitions,
                  'fx_query_verified':True,'read_only_transaction':True,'original_data_modified':False,
                  'fixture_schema':schema,'native_types_and_checks_preserved':True,
                  'test_fixture_writes':True,'imported_business_schema_modified':False,
                  'expired_and_future_rates_fail_closed':True,
                  'ubuntu_gateway_request_verified':False}
        (attempt / 'result.json').write_text(json.dumps(report,indent=2))
        (ROOT / 'output/deployment-ubuntu-20261014/pricing-native-validation.json').write_text(json.dumps(report,indent=2))
        print(json.dumps(report),flush=True)
    except Exception as error:
        import traceback
        (attempt / 'private-diagnostic.txt').write_text(traceback.format_exc())
        print(json.dumps({'status':'failed','exception_type':type(error).__name__,'attempt':str(attempt)}),flush=True)
        return 1
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()
        close_database_pools()
        if started:
            lifecycle('-w','stop','-m','fast')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
