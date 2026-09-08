/**
 * 模块职责：为需要“渲染后唯一记一次”的控制台页面提供通用访问状态机。
 * 维护边界：本模块不读取 Token、不发网络请求，只按调用方给出的页面 ID 和会话代号管理访问生命周期。
 * 来源文件：console-web/src/lib/consoleVisitTracker.js
 */

/**
 * 创建指定页面的访问状态机。
 * generation 隔离离开、重新进入和会话变化；attempted 与 inFlight 同时阻止定时刷新或并发渲染重复上报。
 */
export function createUniquePageVisitTracker({ targetItemId, recordVisit }) {
  let generation = 0;
  let active = false;
  let attempted = false;
  let inFlight = null;
  let sessionKey = null;

  return {
    /**
     * 在页面渲染完成后观察后端确认的 active item；只有从其他页面进入目标页才创建新访问。
     */
    afterRender({ activeItemId, sessionKey: nextSessionKey, source, visitContext }) {
      // Session 变化会使旧代次立即失效；同一 hash 在新认证上下文中应重新记录一次。
      if (nextSessionKey !== sessionKey) {
        generation += 1;
        active = false;
        attempted = false;
        inFlight = null;
        sessionKey = nextSessionKey;
      }
      if (activeItemId !== targetItemId) {
        // 仅在真正离开目标页时推进代次，其他页面的周期刷新不制造无意义状态变化。
        if (active) {
          generation += 1;
          active = false;
          attempted = false;
          inFlight = null;
        }
        return Promise.resolve({ state: "inactive", generation });
      }
      if (!active) {
        generation += 1;
        active = true;
        attempted = false;
        inFlight = null;
      }
      if (inFlight) {
        return inFlight;
      }
      if (attempted) {
        return Promise.resolve({ state: "attempted", generation });
      }
      attempted = true;
      const visitGeneration = generation;
      const request = Promise.resolve().then(() => {
        if (generation !== visitGeneration) return { state: "inactive", generation: visitGeneration };
        return recordVisit({ source, generation: visitGeneration,
          ...(visitContext === undefined ? {} : { visitContext }) });
      });
      inFlight = request.finally(() => {
        // 旧代次请求结束时不能清理新访问的进行中标记，否则会重新打开并发重复上报窗口。
        if (generation === visitGeneration) {
          inFlight = null;
        }
      });
      return inFlight;
    },

    /**
     * 登出、401 或页面销毁时结束当前代次；下次登录或重新进入可以创建新的访问记录。
     */
    leave() {
      generation += 1;
      active = false;
      attempted = false;
      inFlight = null;
      sessionKey = null;
    },

    /**
     * 返回不含认证值的只读快照，供单元测试验证状态机，不暴露 Session 内容。
     */
    snapshot() {
      return { generation, active, attempted, inFlight: Boolean(inFlight) };
    },
  };
}
