/** 按帧提交流事件；终态或主动停止前可同步清空队列，保留事件原顺序。 */
export function createFrameBatch<T>(apply: (items: T[]) => void) {
  let items: T[] = [];
  let frame: number | undefined;
  const cancelFrame = () => {
    if (frame !== undefined) cancelAnimationFrame(frame);
    frame = undefined;
  };
  const flush = () => {
    cancelFrame();
    if (!items.length) return;
    const batch = items;
    items = [];
    apply(batch);
  };
  return {
    push(item: T) {
      items.push(item);
      frame ??= requestAnimationFrame(flush);
    },
    flush,
    cancel() {
      cancelFrame();
      items = [];
    },
  };
}
