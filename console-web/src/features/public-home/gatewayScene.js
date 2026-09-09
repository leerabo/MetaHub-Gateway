import * as THREE from "../../vendor/three.js";

// A local, decorative model of the gateway. No requests or customer data enter this scene.
export function createGatewayScene(host, { onMotionChange = () => {} } = {}) {
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  let paused = reduced.matches;
  let visible = true;
  let destroyed = false;
  let frame = 0;
  let elapsed = 0;
  let last = 0;
  let pointer = 0;
  const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true, powerPreference: "low-power" });
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 1.6));
  renderer.setClearColor(0xf6f7f5, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.domElement.setAttribute("aria-hidden", "true");
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(30, 1, .1, 100);
  camera.position.set(7.5, 9.5, 14);
  camera.lookAt(0, 0, 0);
  const assembly = new THREE.Group();
  scene.add(assembly);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x85968b, 3));
  const key = new THREE.DirectionalLight(0xffffff, 4);
  key.position.set(-3, 10, 6);
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  Object.assign(key.shadow.camera, { left: -12, right: 12, top: 8, bottom: -8 });
  key.shadow.normalBias = .025;
  key.shadow.bias = -.0001;
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xbde2d5, 1.5);
  fill.position.set(5, 4, -7);
  scene.add(fill);
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(200, 200), new THREE.ShadowMaterial({ opacity: .075 }));
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -.35;
  ground.receiveShadow = true;
  scene.add(ground);
  const silver = new THREE.MeshStandardMaterial({ color: 0xc5d0c8, metalness: .65, roughness: .3 });
  const white = new THREE.MeshStandardMaterial({ color: 0xf2f5ef, metalness: .38, roughness: .27 });
  const edge = new THREE.MeshStandardMaterial({ color: 0x295e48, metalness: .5, roughness: .3 });
  const pulseMaterial = new THREE.MeshBasicMaterial({ color: 0x298c67 });
  const plate = roundedPlate(2.4, 2.4, .24, .16);
  for (let i = 0; i < 3; i += 1) {
    const mesh = new THREE.Mesh(plate, i === 1 ? edge : white);
    mesh.position.y = i * .25;
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    assembly.add(mesh);
  }
  const brandCanvas = document.createElement("canvas");
  brandCanvas.width = 256;
  brandCanvas.height = 256;
  const brandContext = brandCanvas.getContext("2d");
  brandContext.fillStyle = "#f2f5ef";
  brandContext.fillRect(0, 0, 256, 256);
  brandContext.fillStyle = "#325842";
  brandContext.textAlign = "center";
  brandContext.textBaseline = "middle";
  brandContext.font = "600 156px Arial";
  brandContext.fillText("M", 128, 128);
  const brandTexture = new THREE.CanvasTexture(brandCanvas);
  brandTexture.colorSpace = THREE.SRGBColorSpace;
  const brandFace = new THREE.Mesh(new THREE.PlaneGeometry(1.65, 1.65), new THREE.MeshStandardMaterial({ map: brandTexture, roughness: .45 }));
  brandFace.rotation.x = -Math.PI / 2;
  brandFace.position.y = .69;
  assembly.add(brandFace);
  const flows = [];
  for (const side of [-1, 1]) {
    for (let lane = -2; lane <= 2; lane += 1) {
      const points = [
        new THREE.Vector3(side * 9, -.04, lane * 1.16),
        new THREE.Vector3(side * 5.8, -.04, lane * 1.16),
        new THREE.Vector3(side * 3.2, .06, lane * .62),
        new THREE.Vector3(side * 1.24, .16, lane * .32),
      ];
      if (side === 1) points.reverse();
      const curve = new THREE.CatmullRomCurve3(points);
      const track = new THREE.Mesh(new THREE.TubeGeometry(curve, 64, .018, 6, false), silver);
      assembly.add(track);
      const trafficGeometry = new THREE.BufferGeometry();
      const positions = new Float32Array(21 * 3);
      trafficGeometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      const traffic = new THREE.Line(trafficGeometry, new THREE.LineBasicMaterial({ color: 0x168463, transparent: true, opacity: .85 }));
      assembly.add(traffic);
      flows.push({ curve, traffic, positions, offset: lane * .18 + (side === 1 ? .5 : 0) });
      const contact = new THREE.Mesh(new THREE.BoxGeometry(.12, .055, .085), pulseMaterial);
      contact.position.set(side * 1.24, .18, lane * .32);
      assembly.add(contact);
      const terminal = new THREE.Mesh(new THREE.BoxGeometry(.48, .07, .38), white);
      terminal.position.set(side * 7.7, .01, lane * 1.16);
      terminal.castShadow = true;
      assembly.add(terminal);
    }
  }
  host.append(renderer.domElement);

  function draw(now = 0) {
    frame = 0;
    if (destroyed) return;
    if (last && !paused) elapsed += Math.min((now - last) / 1000, .06);
    last = now;
    assembly.rotation.y = Math.sin(elapsed * .16) * .028 + pointer * .035;
    assembly.position.y = Math.sin(elapsed * .5) * .025;
    for (const flow of flows) {
      const progress = ((elapsed * .115 + flow.offset) % 1 + 1) % 1;
      for (let i = 0; i <= 20; i += 1) {
        const point = flow.curve.getPoint(Math.min(1, progress + i * .005));
        flow.positions.set([point.x, point.y + .006, point.z], i * 3);
      }
      flow.traffic.geometry.attributes.position.needsUpdate = true;
      flow.traffic.geometry.computeBoundingSphere();
    }
    renderer.render(scene, camera);
    host.dataset.rendered = "true";
    schedule();
  }
  function schedule() {
    if (!frame && !paused && visible && !document.hidden && !destroyed) frame = requestAnimationFrame(draw);
  }
  function stopFrame() {
    cancelAnimationFrame(frame);
    frame = 0;
    last = 0;
  }
  function resize() {
    const { width, height } = host.getBoundingClientRect();
    if (!width || !height || destroyed) return;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    const distance = width < 600 ? 24 : 19;
    camera.position.set(distance * .29, distance * .48, distance * .76);
    camera.lookAt(0, .08, 0);
    camera.updateProjectionMatrix();
    stopFrame();
    draw();
  }
  function setPaused(value) {
    paused = Boolean(value);
    stopFrame();
    onMotionChange(paused);
    schedule();
    return paused;
  }
  function preferenceChanged() { setPaused(reduced.matches); }
  function visibilityChanged() { stopFrame(); schedule(); }
  function move(event) {
    if (paused) return;
    const bounds = host.getBoundingClientRect();
    pointer = (event.clientX - bounds.left) / bounds.width - .5;
  }
  function contextLost(event) {
    event.preventDefault();
    destroy();
  }
  const resizeObserver = new ResizeObserver(resize);
  resizeObserver.observe(host);
  const intersectionObserver = new IntersectionObserver(([entry]) => {
    visible = entry.isIntersecting;
    stopFrame();
    schedule();
  });
  intersectionObserver.observe(host);
  document.addEventListener("visibilitychange", visibilityChanged);
  reduced.addEventListener("change", preferenceChanged);
  host.addEventListener("pointermove", move, { passive: true });
  renderer.domElement.addEventListener("webglcontextlost", contextLost);
  resize();
  onMotionChange(paused);

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    stopFrame();
    resizeObserver.disconnect();
    intersectionObserver.disconnect();
    document.removeEventListener("visibilitychange", visibilityChanged);
    reduced.removeEventListener("change", preferenceChanged);
    host.removeEventListener("pointermove", move);
    renderer.domElement.removeEventListener("webglcontextlost", contextLost);
    const geometries = new Set();
    const materials = new Set();
    scene.traverse((object) => {
      if (object.geometry) geometries.add(object.geometry);
      for (const material of [object.material].flat().filter(Boolean)) materials.add(material);
    });
    geometries.forEach((geometry) => geometry.dispose());
    materials.forEach((material) => material.dispose());
    brandTexture.dispose();
    renderer.dispose();
    renderer.domElement.remove();
    delete host.dataset.rendered;
  }
  return { setPaused, get paused() { return paused; }, destroy };
}

function roundedPlate(width, height, radius, depth) {
  const shape = new THREE.Shape();
  const x = -width / 2;
  const y = -height / 2;
  shape.moveTo(x + radius, y);
  shape.lineTo(x + width - radius, y);
  shape.quadraticCurveTo(x + width, y, x + width, y + radius);
  shape.lineTo(x + width, y + height - radius);
  shape.quadraticCurveTo(x + width, y + height, x + width - radius, y + height);
  shape.lineTo(x + radius, y + height);
  shape.quadraticCurveTo(x, y + height, x, y + height - radius);
  shape.lineTo(x, y + radius);
  shape.quadraticCurveTo(x, y, x + radius, y);
  const geometry = new THREE.ExtrudeGeometry(shape, { depth, bevelEnabled: true, bevelSegments: 3, steps: 1, bevelSize: .028, bevelThickness: .024 });
  geometry.rotateX(-Math.PI / 2);
  return geometry;
}
