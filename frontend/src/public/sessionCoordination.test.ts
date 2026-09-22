import { afterEach, describe, expect, it, vi } from "vitest";

import {
  openPublicSessionChannel,
  recordPublicIdentity,
  type PublicSessionEvent,
} from "./sessionCoordination";

class FakeBroadcastChannel {
  private static readonly channels = new Map<
    string,
    Set<FakeBroadcastChannel>
  >();

  private readonly listeners = new Set<
    (message: MessageEvent<unknown>) => void
  >();

  constructor(private readonly name: string) {
    const members = FakeBroadcastChannel.channels.get(name) ?? new Set();
    members.add(this);
    FakeBroadcastChannel.channels.set(name, members);
  }

  addEventListener(
    _type: "message",
    listener: (message: MessageEvent<unknown>) => void,
  ) {
    this.listeners.add(listener);
  }

  postMessage(data: unknown) {
    for (const member of FakeBroadcastChannel.channels.get(this.name) ?? []) {
      if (member === this) continue;
      for (const listener of member.listeners) {
        listener(new MessageEvent("message", { data }));
      }
    }
  }

  close() {
    FakeBroadcastChannel.channels.get(this.name)?.delete(this);
  }
}

afterEach(() => {
  window.localStorage.clear();
  vi.unstubAllGlobals();
});

describe("公共会话多标签协调", () => {
  it("按 deployment id 隔离退出通知", () => {
    vi.stubGlobal("BroadcastChannel", FakeBroadcastChannel);
    const sameDeployment: PublicSessionEvent[] = [];
    const otherDeployment: PublicSessionEvent[] = [];
    const source = openPublicSessionChannel("candidate_8289", () => undefined);
    const peer = openPublicSessionChannel("candidate_8289", (event) => {
      sameDeployment.push(event);
    });
    const other = openPublicSessionChannel("production_8288", (event) => {
      otherDeployment.push(event);
    });

    source.publish("logout");

    expect(sameDeployment).toEqual(["logout"]);
    expect(otherDeployment).toEqual([]);
    source.close();
    peer.close();
    other.close();
  });

  it("只在同一部署的用户身份实际变化时报告切换", () => {
    expect(recordPublicIdentity("candidate_8289", "user-a")).toBe(false);
    expect(recordPublicIdentity("candidate_8289", "user-a")).toBe(false);
    expect(recordPublicIdentity("candidate_8289", "user-b")).toBe(true);
    expect(recordPublicIdentity("production_8288", "user-b")).toBe(false);
  });
});
