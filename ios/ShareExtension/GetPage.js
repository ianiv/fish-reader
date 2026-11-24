// Share-extension preprocessing: runs in the Safari page (where you're signed in) and hands the page's
// URL, title and full HTML to the extension, so paywalled/member posts can be read too.
var GetPage = function () {};
GetPage.prototype = {
  run: function (args) {
    args.completionFunction({
      url: document.URL,
      title: document.title,
      html: document.documentElement.outerHTML
    });
  },
  finalize: function () {}
};
var ExtensionPreprocessingJS = new GetPage();
