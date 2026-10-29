import assert from "node:assert/strict";
import test from "node:test";
import { gatewayServiceOrigin, isDirectServiceHost, serviceOrigin } from "../../src/lib/runtimeOrigins.js";

test("direct service hosts include loopback and RFC1918 IPv4 only", () => {
  for (const hostname of ["localhost", "127.0.0.1", "::1", "10.0.0.7", "172.16.100.2", "172.31.255.255", "192.168.1.8"]) {
    assert.equal(isDirectServiceHost(hostname), true, hostname);
  }
  for (const hostname of ["172.15.0.1", "172.32.0.1", "8.8.8.8", "metahub.example", "s1.localhost", "172.16.1.999"]) {
    assert.equal(isDirectServiceHost(hostname), false, hostname);
  }
});

test("private-address trial resolves companion service ports while domain deployments stay same-origin", () => {
  const lan = { protocol: "http:", hostname: "172.16.100.2", origin: "http://172.16.100.2:5173" };
  assert.equal(serviceOrigin(lan, 8001), "http://172.16.100.2:8001");
  assert.equal(gatewayServiceOrigin(lan), "http://172.16.100.2:8000");
  const domain = { protocol: "https:", hostname: "trial.metahub.example", origin: "https://trial.metahub.example" };
  assert.equal(serviceOrigin(domain, 8001), domain.origin);
  assert.equal(gatewayServiceOrigin(domain), domain.origin);
});

test("module-only consumers retain the local Control origin without a browser location", () => {
  assert.equal(serviceOrigin(undefined, 8001), "http://localhost:8001");
});

test("HTTPS ingress never escapes to fixed companion ports on private or loopback hosts", () => {
  for (const hostname of ["localhost", "127.0.0.1", "172.16.100.2"]) {
    const location = { protocol: "https:", hostname, origin: `https://${hostname}:8446` };
    assert.equal(serviceOrigin(location, 8001), location.origin);
    assert.equal(gatewayServiceOrigin(location), location.origin);
  }
});
