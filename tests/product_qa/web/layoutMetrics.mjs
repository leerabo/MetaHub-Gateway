/** Runs in the browser document; records geometry only, never field values. */
export function measureInteractiveLayout() {
  const paintedRects = new Map();
  const paintedRect = (element) => {
    const bounds = element.getBoundingClientRect();
    const rect = { left: bounds.left, right: bounds.right, top: bounds.top, bottom: bounds.bottom };
    // Full-page audits include content below the browser viewport, but nested
    // scroll/clip containers still hide descendants outside their client area.
    for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
      if (ancestor === document.scrollingElement) continue;
      const style = getComputedStyle(ancestor);
      const clipX = ["auto", "scroll", "hidden", "clip"].includes(style.overflowX);
      const clipY = ["auto", "scroll", "hidden", "clip"].includes(style.overflowY);
      if (!clipX && !clipY) continue;
      const box = ancestor.getBoundingClientRect();
      const scaleX = ancestor.offsetWidth ? box.width / ancestor.offsetWidth : 1;
      const scaleY = ancestor.offsetHeight ? box.height / ancestor.offsetHeight : 1;
      const left = box.left + ancestor.clientLeft * scaleX;
      const top = box.top + ancestor.clientTop * scaleY;
      if (clipX) {
        rect.left = Math.max(rect.left, left);
        rect.right = Math.min(rect.right, left + ancestor.clientWidth * scaleX);
      }
      if (clipY) {
        rect.top = Math.max(rect.top, top);
        rect.bottom = Math.min(rect.bottom, top + ancestor.clientHeight * scaleY);
      }
    }
    return rect;
  };
  const visible = [...document.querySelectorAll("button, input, select, textarea, a[href]")]
    .filter((element) => {
      // Closed details retain descendant layout boxes in Chromium. A box alone
      // does not mean the control is painted or available to the user.
      if (!element.checkVisibility({ visibilityProperty: true, opacityProperty: true })) return false;
      const rect = paintedRect(element);
      paintedRects.set(element, rect);
      return rect.right > rect.left && rect.bottom > rect.top;
    });
  const clipped = visible.filter((element) => (
    ["BUTTON", "A"].includes(element.tagName)
    && (element.scrollWidth > element.clientWidth + 1 || element.scrollHeight > element.clientHeight + 1)
  ));
  let overlaps = 0;
  const overlapPairs = [];
  for (let leftIndex = 0; leftIndex < visible.length; leftIndex += 1) {
    const left = visible[leftIndex];
    const leftRect = paintedRects.get(left);
    for (let rightIndex = leftIndex + 1; rightIndex < visible.length; rightIndex += 1) {
      const right = visible[rightIndex];
      if (left.contains(right) || right.contains(left)) continue;
      const rightRect = paintedRects.get(right);
      const overlapWidth = Math.min(leftRect.right, rightRect.right) - Math.max(leftRect.left, rightRect.left);
      const overlapHeight = Math.min(leftRect.bottom, rightRect.bottom) - Math.max(leftRect.top, rightRect.top);
      if (overlapWidth > 2 && overlapHeight > 2) {
        overlaps += 1;
        if (overlapPairs.length < 12) {
          const describe = (element) => ({
            tag: element.tagName,
            id: element.id,
            name: element.getAttribute("name"),
            type: element.getAttribute("type"),
            bounds: element.getBoundingClientRect().toJSON(),
            clipping_ancestors: [...function* () {
              for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
                const style = getComputedStyle(ancestor);
                if ([style.overflowX, style.overflowY].some(value => ["auto", "scroll", "hidden", "clip"].includes(value))) {
                  yield { tag: ancestor.tagName, className: ancestor.className, overflowX: style.overflowX, overflowY: style.overflowY, bounds: ancestor.getBoundingClientRect().toJSON() };
                }
              }
            }()],
            text: String(element.textContent || "").trim().replace(/\s+/gu, " ").slice(0, 80),
          });
          overlapPairs.push([describe(left), describe(right)]);
        }
      }
    }
  }
  return { clipped_control_count: clipped.length, interactive_overlap_count: overlaps, overlap_pairs: overlapPairs };
}
