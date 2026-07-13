/** Agent 对话 Markdown 渲染（marked + DOMPurify，CDN 加载） */
(function (global) {
  var markedReady = false;

  function escapeHtml(s) {
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  function initMarked() {
    if (markedReady || typeof marked === 'undefined') return markedReady;
    var renderer = new marked.Renderer();
    var origLink = renderer.link.bind(renderer);
    renderer.link = function (href, title, text) {
      var html = origLink(href, title, text);
      return html.replace(/^<a /, '<a target="_blank" rel="noopener noreferrer" ');
    };
    marked.setOptions({ breaks: true, gfm: true, renderer: renderer });
    markedReady = true;
    return true;
  }

  function renderAgentMarkdown(text) {
    if (!text) return '';
    if (!initMarked()) {
      return escapeHtml(String(text)).replace(/\n/g, '<br>');
    }
    var raw = marked.parse(String(text));
    if (typeof DOMPurify !== 'undefined') {
      return DOMPurify.sanitize(raw);
    }
    return raw;
  }

  global.renderAgentMarkdown = renderAgentMarkdown;
})(window);
