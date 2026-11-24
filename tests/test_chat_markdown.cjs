const assert = require("node:assert/strict");
const test = require("node:test");

globalThis.markdownit = require("../web/assets/vendor/markdown-it-15.0.3.min.js");
require("../web/assets/chat-markdown.js");
const render = globalThis.renderChatMarkdown;

test("renders formatting, code, real lists, and compact dash items", () => {
  const source = [
    "## 驗證結果", "收到，**高處落地（`y=1.0592`）** 完成。",
    "- **第一項**：使用 `ackTick`", "-第二項  -第三項 `-keep  -spacing`", "",
    "1. *斜體*", "2. ~~已移除~~", "", "- [x] 完成", "- [ ] 待測", "",
    "> 引用", "", "| 項目 | 狀態 |", "| --- | --- |", "| 測試 | 通過 |", "",
    "```txt", "-Flag  -Keep", "@all <script>", "```",
  ].join("\n");
  const html = render(source);
  assert.match(html, /<h2>驗證結果<\/h2>/);
  assert.match(html, /<strong>高處落地（<code>y=1\.0592<\/code>）<\/strong>/);
  assert.match(html, /<li>第二項<\/li>\n<li>第三項 <code>-keep  -spacing<\/code><\/li>/);
  assert.match(html, /<ol>\n<li><em>斜體<\/em><\/li>\n<li><s>已移除<\/s><\/li>/);
  assert.match(html, /class="md-task" type="checkbox" disabled checked/);
  assert.match(html, /class="md-task" type="checkbox" disabled aria-label="未完成"/);
  assert.match(html, /<blockquote>/);
  assert.match(html, /<table>/);
  assert.match(html, /<pre><code>-Flag  -Keep\n@all &lt;script&gt;/);
  assert.doesNotMatch(render("--help\n-29"), /<ul>/);
});

test("keeps search and mentions outside HTML attributes and leaves code mentions literal", () => {
  const html = render("@codex-gpt-6 **codex** `@codex`", "codex");
  assert.match(html, /<span class="mention">@<mark>codex<\/mark>-gpt-6<\/span>/);
  assert.match(html, /<strong><mark>codex<\/mark><\/strong>/);
  assert.match(html, /<code>@<mark>codex<\/mark><\/code>/);
  assert.match(render("**a&b**", "&"), /<strong>a<mark>&amp;<\/mark>b<\/strong>/);
});

test("renders raw HTML as text and excludes script, file, and image loading", () => {
  const html = render([
    '<img src="invalid" onerror="alert(1)">',
    '[bad](javascript:alert(1)) [local](file:///C:/secret)',
    '![diagram](https://example.org/diagram.png) [docs](https://example.org)',
  ].join("\n"));
  assert.doesNotMatch(html, /<img|<script|href="(?:javascript:|file:)/);
  assert.match(html, /&lt;img/);
  assert.match(html, /<a href="https:\/\/example.org\/diagram.png" target="_blank" rel="noopener noreferrer">diagram<\/a>/);
});
