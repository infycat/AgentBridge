/* Render AgentBridge chat text using the bundled Markdown parser. */
(() => {
  "use strict";

  const escape = (text) => text.replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);

  function markText(text, search) {
    if (!search) return escape(text);
    let html = "", from = 0;
    for (const match of text.matchAll(search)) {
      html += escape(text.slice(from, match.index)) + `<mark>${escape(match[0])}</mark>`;
      from = match.index + match[0].length;
    }
    return html + escape(text.slice(from));
  }

  function chatText(text, search) {
    let html = "", from = 0;
    for (const mention of text.matchAll(/@([\w.-]+|全體)/g)) {
      html += markText(text.slice(from, mention.index), search);
      html += `<span class="mention">${markText(mention[0], search)}</span>`;
      from = mention.index + mention[0].length;
    }
    return html + markText(text.slice(from), search);
  }

  function compactLists(source) {
    let fence = "";
    return source.split("\n").map((line) => {
      const marker = /^ {0,3}(?:(?:[-+*]|\d{1,9}[.)])[ \t]+)?(`{3,}|~{3,})(.*)$/.exec(line);
      if (fence) {
        if (marker && marker[1][0] === fence[0] && marker[1].length >= fence.length && !marker[2].trim()) fence = "";
        return line;
      }
      if (marker && (marker[1][0] === "~" || !marker[2].includes("`"))) { fence = marker[1]; return line; }
      const bullet = /^( {0,3})-(?:[ \t]+(?=\S)|(?=[\p{L}*`\[]))/u;
      const first = bullet.exec(line);
      if (!first) return line;

      // Treat two or more spaces before a further dash as a new item on a bullet line.
      let ticks = 0;
      const body = line.slice(first[0].length).replace(/\\[\\`-]|`+|[ \t]{2,}-(?:[ \t]+(?=\S)|(?=[\p{L}*`\[]))/gu, (part) => {
        if (part[0] === "`") {
          if (!ticks) ticks = part.length;
          else if (ticks === part.length) ticks = 0;
          return part;
        }
        return !ticks && /^[ \t]/.test(part) ? `\n${first[1]}- ` : part;
      });
      const prefix = first[0].endsWith("-") ? `${first[0]} ` : first[0];
      return prefix + body;
    }).join("\n");
  }

  const parser = typeof globalThis.markdownit === "function"
    ? globalThis.markdownit({ html: false, breaks: true, linkify: true }) : null;

  if (parser) {
    const validateLink = parser.validateLink;
    parser.validateLink = (href) => /^(https?:\/\/|mailto:)/i.test(href) && validateLink(href);
    parser.core.ruler.after("normalize", "chat_compact_bullets", (state) => {
      state.src = compactLists(state.src);
    });
    parser.core.ruler.after("inline", "chat_task_items", ({ tokens }) => {
      for (let i = 2; i < tokens.length; i++) {
        if (tokens[i].type !== "inline" || tokens[i - 2].type !== "list_item_open" || tokens[i - 1].type !== "paragraph_open") continue;
        const first = tokens[i].children[0];
        const task = first?.type === "text" && /^\[([ xX])\][ \t]+/.exec(first.content);
        if (!task) continue;
        tokens[i - 2].attrJoin("class", "md-task-item");
        first.content = first.content.slice(task[0].length);
        first.meta = { chatTask: task[1] !== " " };
      }
    });
    parser.renderer.rules.text = (tokens, i, _options, { search }) => {
      const token = tokens[i];
      const checked = token.meta?.chatTask;
      const task = checked === undefined ? "" : `<input class="md-task" type="checkbox" disabled${checked ? ' checked aria-label="已完成"' : ' aria-label="未完成"'}> `;
      return task + chatText(token.content, search);
    };
    parser.renderer.rules.code_inline = (tokens, i, _options, { search }) => `<code>${markText(tokens[i].content, search)}</code>`;
    parser.renderer.rules.fence = parser.renderer.rules.code_block = (tokens, i, _options, { search }) =>
      `<pre><code>${markText(tokens[i].content, search)}</code></pre>\n`;
    parser.renderer.rules.link_open = (tokens, i, options, _env, renderer) => {
      tokens[i].attrSet("target", "_blank");
      tokens[i].attrSet("rel", "noopener noreferrer");
      return renderer.renderToken(tokens, i, options);
    };
    // Images are links so opening a chat cannot initiate third-party requests.
    parser.renderer.rules.image = (tokens, i, _options, { search }) => {
      const token = tokens[i];
      const url = token.attrGet("src");
      const label = markText(token.content || "圖片", search);
      return url ? `<a href="${escape(url)}" target="_blank" rel="noopener noreferrer">${label}</a>` : label;
    };
    parser.renderer.rules.table_open = () => '<div class="md-table"><table>\n';
    parser.renderer.rules.table_close = () => "</table></div>\n";
  }

  globalThis.renderChatMarkdown = (value, query = "") => {
    const text = String(value ?? "");
    const search = query ? new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi") : null;
    return parser ? parser.render(text, { search }) : chatText(text, search).replace(/\r?\n/g, "<br>");
  };
})();
