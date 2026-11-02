/* Draw the math on this page. The server wrote it between \\( \\) and \\[ \\]. */
(function () {
  "use strict";

  window.renderMathInElement(document.body, {
    delimiters: [
      { left: "\\(", right: "\\)", display: false },
      { left: "\\[", right: "\\]", display: true },
    ],
    throwOnError: false,
  });
})();
