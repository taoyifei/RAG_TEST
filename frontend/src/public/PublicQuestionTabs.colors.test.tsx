import { readFileSync } from "node:fs";

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PublicQuestionTabs } from "./PublicQuestionTabs";

const styles = readFileSync("src/styles.css", "utf8");
let stylesheet: HTMLStyleElement;

beforeEach(() => {
  stylesheet = document.createElement("style");
  stylesheet.textContent = styles;
  document.head.append(stylesheet);
});
afterEach(() => stylesheet.remove());

// jsdom 不计算 var() 的背景色；直接解析真实规则并确认引用的主题值存在。
function tabBackground(selected: boolean, root: Element): string {
  const selector = `.wst-question-tab-buttons button${selected ? '[aria-selected="true"]' : ""}`;
  const rule = Array.from(stylesheet.sheet?.cssRules ?? []).find(
    (item): item is CSSStyleRule =>
      "selectorText" in item && item.selectorText === selector,
  );
  expect(rule).toBeDefined();
  const value = rule?.style.getPropertyValue("background") ?? "";
  const token = /^var\((--[a-z-]+)\)$/.exec(value)?.[1];
  return token ? getComputedStyle(root).getPropertyValue(token).trim() : value;
}

describe.each([
  {
    theme: "light",
    foreground: "rgb(32, 43, 39)",
    panel: "#fff",
  },
  { theme: "dark", foreground: "rgb(242, 245, 244)", panel: "#303030" },
])("$theme 推荐标签", ({ theme, foreground, panel }) => {
  it("两个标签切换后都有深绿选中底色和清楚的文字", async () => {
    const user = userEvent.setup();
    render(
      <div className="wst-root" data-theme={theme}>
        <PublicQuestionTabs
          busy={false}
          onRefresh={vi.fn()}
          onPopularSubmit={vi.fn()}
          onSuggestedSubmit={vi.fn()}
          popular={{
            mode: "POPULAR",
            generated_at: null,
            window_days: 7,
            items: [
              { id: "public-1", question: "如何上传资料？", topic_key: "资料" },
            ],
          }}
          suggestions={[
            {
              id: "suggested-1",
              question: "如何查询制度？",
              topicKey: "制度",
              documentId: "public",
              style: "SHORT",
              enabled: true,
            },
          ]}
        />
      </div>,
    );
    const suggested = screen.getByRole("tab", { name: "你可能想问" });
    const popular = screen.getByRole("tab", { name: "大家常问" });
    for (const [selected, unselected] of [
      [suggested, popular],
      [popular, suggested],
    ]) {
      await user.click(selected);
      expect(selected).toHaveAttribute("aria-selected", "true");
      const root = selected.closest(".wst-root");
      expect(root).not.toBeNull();
      expect(tabBackground(true, root!)).toBe("#0b2f29");
      expect(getComputedStyle(selected).color).toBe("rgb(255, 255, 255)");
      expect(tabBackground(false, root!)).toBe(panel);
      expect(getComputedStyle(unselected).color).toBe(foreground);
    }
  });
});
