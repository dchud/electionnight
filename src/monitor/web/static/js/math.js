/* Draw the math on this page. The server wrote it between \( \) and \[ \].
   A view that replaces its content draws again with `drawMath`. */
(function () {
  "use strict";

  window.drawMath = function (element) {
    window.renderMathInElement(element, {
      delimiters: [
        { left: "\\(", right: "\\)", display: false },
        { left: "\\[", right: "\\]", display: true },
      ],
      throwOnError: false,
    });
  };
  window.drawMath(document.body);
})();
