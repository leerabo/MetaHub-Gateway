"""Capture own Ubuntu pages in both themes; never equate capture with review.

Use only after a fresh Playwright CLI snapshot and own-account authentication.
Credentials and input values are neither requested nor collected by this tool.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://trial.local:8446"
WORK = ROOT / "output/playwright/trial-ubuntu"

CAPTURE = r"""async (page) => {
  if (new URL(page.url()).origin !== ORIGIN) throw new Error('Not the independent Ubuntu fixture');
  const runtime = await page.evaluate(async () => {
    const response = await fetch('/version');
    if (!response.ok) throw new Error('Runtime version unavailable');
    return (await response.json()).version;
  });
  if (runtime !== RELEASE) throw new Error('Runtime does not match the frozen page inventory');
  const captures = [];
  for (const row of ROWS) {
    try {
      await page.goto(ORIGIN + '/' + row.url, { waitUntil: 'domcontentloaded', timeout: 45000 });
    } catch (error) {
      captures.push({route: row.route, surface: row.surface, status: 'navigation_timeout_requires_review',
        requestedUrl: row.url, error: String(error).slice(0, 400), visuallyReviewed: false,
        interactionsReviewed: []});
      continue;
    }
    let idle = true;
    await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => { idle = false; });
    const requested = new URL(ORIGIN + '/' + row.url);
    if (new URL(page.url()).pathname !== requested.pathname) {
      captures.push({route: row.route, status: 'redirect_requires_review', pathname: new URL(page.url()).pathname});
      continue;
    }
    // A pathname and a loading skeleton do not establish that an account view
    // rendered. Wait before looking for its theme button, otherwise a slow
    // bootstrap can cause an unnecessary reload that starts it all over again.
    const identityView = row.surface === 'identity';
    if (identityView) {
      await page.waitForFunction(route =>
        document.querySelector('[data-account-section]')?.dataset.accountSection === route,
        row.route, {timeout:90000}).catch(() => {});
      if (row.route === 'preferences') {
        await page.waitForFunction(() => {
          const section = document.querySelector('#account-preferences');
          return section?.querySelector('[data-account-preference-form], [role="alert"]');
        }, null, {timeout:90000}).catch(() => {});
      }
    }
    for (const theme of ['dark', 'light']) {
      let themeMethod = 'existing_preference';
      let active = await page.evaluate(() => document.documentElement.dataset.theme);
      if (active !== theme) {
        const button = page.locator('[data-theme-toggle]').first();
        if (!await button.count()) {
          // Public auth pages consume the shared saved appearance preference.
          // Reload through the real initializer; do not inject CSS or dataset.
          await page.evaluate(value => localStorage.setItem('metahub.appearance.theme', value), theme);
          await page.reload({waitUntil: 'domcontentloaded', timeout: 45000}).catch(() => { idle = false; });
          await page.waitForLoadState('networkidle', {timeout: 30000}).catch(() => { idle = false; });
          themeMethod = 'saved_preference_and_real_reload';
        } else {
          await button.click();
          themeMethod = 'appearance_button';
        }
        await page.waitForFunction(value => document.documentElement.dataset.theme === value, theme).catch(() => {});
      }
      await page.evaluate(() => document.fonts.ready);
      await page.evaluate(() => window.scrollTo(0, 0));
      const expectedRoute = requested.hash.slice(1);
      const routeSelector = row.surface === 'platform'
        ? '[data-platform-view][aria-current="page"]'
        : row.surface === 'workspace' ? '[data-console-route][aria-current="page"]'
        : identityView ? '[data-account-section]' : null;
      if (routeSelector) {
        await page.waitForFunction(({selector, route}) => {
          const current = document.querySelector(selector);
          return (current?.dataset.platformView ?? current?.dataset.consoleRoute ?? current?.dataset.accountSection) === route;
        }, {selector:routeSelector, route:expectedRoute}, {timeout:30000}).catch(() => {});
      }
      if (row.surface === 'platform') {
        await page.waitForFunction(() =>
          document.querySelector('.platform-content')?.getAttribute('aria-busy') === 'false'
          && !document.querySelector('.platform-content [aria-busy="true"]')
          && ![...document.querySelectorAll('[data-platform-commercial-root] td')]
            .some(element => ['正在读取套餐','正在读取参考成本'].includes(element.textContent.trim()))
          && ![...document.querySelectorAll('.console-experience-state')]
            .some(element => element.textContent.includes('正在读取当前页面'))
          && ![...document.querySelectorAll('.platform-content [role="status"], .platform-content .platform-loading, .platform-content .platform-feedback')]
            .some(element => element.getClientRects().length
              && /^(正在读取|正在加载|正在获取|正在同步|正在检查)/.test(element.textContent.trim())),
          null, {timeout:90000}).catch(() => {});
      }
      const facts = await page.evaluate(() => {
        const visible = element => {
          const rect = element.getBoundingClientRect();
          const style = getComputedStyle(element);
          return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
        };
        const clippedButtons = [...document.querySelectorAll('button, [role="button"]')]
          .filter(visible).filter(element => element.scrollWidth > element.clientWidth + 2)
          .map(element => ({text: element.textContent.trim().slice(0, 45), width: element.clientWidth, contentWidth: element.scrollWidth}));
        const dialogs = [...document.querySelectorAll('dialog[open], [role="dialog"]')].filter(visible)
          .map(element => {
            const rect = element.getBoundingClientRect();
            return {top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right,
              fitsViewport: rect.top >= 0 && rect.left >= 0 && rect.right <= innerWidth && rect.bottom <= innerHeight};
          });
        const activePlatform = document.querySelector('[data-platform-view][aria-current="page"]');
        const activeWorkspace = document.querySelector('[data-console-route][aria-current="page"]');
        const activeIdentity = document.querySelector('[data-account-section]');
        return {title: document.title, heading: document.querySelector('h1')?.textContent.trim(),
          renderedRoute: activePlatform?.dataset.platformView ?? activeWorkspace?.dataset.consoleRoute ?? activeIdentity?.dataset.accountSection ?? null,
          theme: document.documentElement.dataset.theme, viewport: {width: innerWidth, height: innerHeight},
          bodyWidth: document.documentElement.scrollWidth, bodyHeight: document.documentElement.scrollHeight,
          horizontalPageOverflow: document.documentElement.scrollWidth > innerWidth + 1,
          clippedButtons, dialogs, tableCount: document.querySelectorAll('table').length,
          loginFormVisible: !!document.querySelector('[data-identity-login-form]'),
          preferenceFormReady: !!document.querySelector('[data-account-preference-form]'),
          platformBusy: document.querySelector('.platform-content')?.getAttribute('aria-busy') === 'true',
          nestedPlatformBusy: !!document.querySelector('.platform-content [aria-busy="true"]'),
          commercialLoading: [...document.querySelectorAll('[data-platform-commercial-root] td')]
            .some(element => ['正在读取套餐','正在读取参考成本'].includes(element.textContent.trim())),
          consoleExperienceLoading: [...document.querySelectorAll('.console-experience-state')]
            .some(element => element.textContent.includes('正在读取当前页面')),
          systemUpdatesLoading: [...document.querySelectorAll('[data-system-updates]')]
            .some(element => element.getAttribute('aria-busy') === 'true'
              || element.textContent.trim().startsWith('加载系统升级状态')),
          pendingStatusMessages: [...document.querySelectorAll('.platform-content [role="status"], .platform-content .platform-loading, .platform-content .platform-feedback')]
            .filter(visible).map(element => element.textContent.trim())
            .filter(text => /^(正在读取|正在加载|正在获取|正在同步|正在检查)/.test(text)),
          alerts: [...document.querySelectorAll('[role="alert"]')].filter(visible)
            .map(element => element.textContent.trim().slice(0, 150))};
      });
      // Forms containing one-time credentials must be closed/cleared before capture.
      // Password values are not read; refuse any populated password control.
      const populatedPassword = await page.locator('input[type="password"]').evaluateAll(elements => elements.some(element => !!element.value));
      if (populatedPassword) throw new Error('Clear password fields before screenshots');
      const stem = row.surface + '-' + row.route.replace(/[^a-zA-Z0-9.-]/g, '-') + '-' + theme;
      const viewportPath = DIRECTORY + '/' + stem + '-viewport.png';
      const fullPath = DIRECTORY + '/' + stem + '-full.png';
      await page.screenshot({path: viewportPath, animations: 'disabled'});
      await page.screenshot({path: fullPath, fullPage: true, animations: 'disabled'});
      const routeMatches = routeSelector ? facts.renderedRoute === expectedRoute : null;
      const contentPending = facts.platformBusy || facts.nestedPlatformBusy || facts.commercialLoading || facts.consoleExperienceLoading || facts.systemUpdatesLoading || facts.pendingStatusMessages.length > 0 || (identityView && row.route === 'preferences'
        && !facts.preferenceFormReady && facts.alerts.length === 0);
      captures.push({route: row.route, surface: row.surface, theme,
        status: routeMatches === false ? 'route_mismatch_requires_review'
          : contentPending ? 'loading_requires_recapture' : 'captured_pending_visual_review',
        routeMatches,
        requestedUrl: row.url, actualPath: new URL(page.url()).pathname, actualHash: new URL(page.url()).hash,
        themeMethod,
        networkIdleReached: idle, viewportScreenshot: viewportPath, fullScreenshot: fullPath,
        visuallyReviewed: false, interactionsReviewed: [], facts});
    }
  }
  return {status: 'captures_pending_manual_visual_review', release: RELEASE, captures,
    allPagesVerified: false, credentialsCollected: false};
}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checklist', type=Path, required=True)
    parser.add_argument('--surface', choices=('workspace', 'platform', 'identity', 'identity-auth'), required=True)
    parser.add_argument('--start', type=int, default=0)
    parser.add_argument('--count', type=int, default=2)
    parser.add_argument('--session', choices=('metahub-ubuntu-trial', 'metahub-ubuntu-anon'), default='metahub-ubuntu-trial')
    args = parser.parse_args()
    if args.start < 0 or not 1 <= args.count <= 4:
        parser.error('Use a nonnegative start and one to four routes per reviewed batch')
    checklist = json.loads(args.checklist.read_text(encoding='utf-8'))
    rows = [row for row in checklist['checklist'] if row['surface'] == args.surface]
    selected = rows[args.start:args.start + args.count]
    if not selected:
        parser.error('No routes in this batch')
    allowed = {'workspace': 'console.html#', 'platform': 'platform.html#',
               'identity': 'identity.html?view=account#', 'identity-auth': 'identity.html?view='}
    if not all(row['url'].startswith(allowed[args.surface]) and '\\' not in row['url']
               and '..' not in row['url'] and '\n' not in row['url'] for row in selected):
        raise ValueError('Only inventoried fixture-local page routes are allowed')
    release = checklist['release']
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')
    artifacts = WORK / 'visual-pages' / release / f'{args.surface}-{args.start:03d}-{stamp}'
    artifacts.mkdir(parents=True, exist_ok=False)
    script = CAPTURE
    for token, value in (('ORIGIN', ORIGIN), ('ROWS', selected), ('DIRECTORY', artifacts.as_posix()), ('RELEASE', release)):
        script = script.replace(token, json.dumps(value, ensure_ascii=False))
    source = artifacts / 'capture.js'
    source.write_text(script, encoding='utf-8')
    node = Path('C:/Program Files/nodejs/node.exe')
    npx = node.parent / 'node_modules/npm/bin/npx-cli.js'
    command = [str(node), str(npx), '--offline', '--yes', '--package', '@playwright/cli', 'playwright-cli',
               '-s=' + args.session, 'run-code', '--filename', str(source)]
    result = subprocess.run(command, cwd=WORK, capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=600)
    (artifacts / 'cli-output.txt').write_text(result.stdout + '\n' + result.stderr, encoding='utf-8')
    report = {'status': 'capture_failed', 'exit': result.returncode, 'release': release,
              'routes_requested': [row['route'] for row in selected], 'all_pages_verified': False}
    marker = '### Result\n'
    if result.returncode == 0 and marker in result.stdout:
        payload = result.stdout.split(marker, 1)[1].split('\n### ', 1)[0].strip()
        parsed = json.loads(payload)
        if not isinstance(parsed, dict) or parsed.get('status') != 'captures_pending_manual_visual_review':
            raise ValueError('Unexpected capture result; no review status was updated')
        report.update(parsed)
    (artifacts / 'capture-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': report['status'], 'report': str(artifacts / 'capture-report.json'),
                      'visually_reviewed': 0, 'all_pages_verified': False}))
    return result.returncode if result.returncode else 0 if 'captures' in report else 1


if __name__ == '__main__':
    raise SystemExit(main())
