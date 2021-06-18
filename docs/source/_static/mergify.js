$(function() {
  // The default href for the current navbar on load is # which does not match the first h1 title and breaks scrollspy
  $("li.toctree-l1.current > a[href='#']").attr("href", $(".section > h1 > a").attr('href'));
  $("li.toctree-l1.current > a[href='#']").attr("href", $("section > h1 > a").attr('href'));

  // This is a bit tricky: scrollspy wants precisely a list where all anchors
  // are starting with "#" so we need to select precisely in the navbar where
  // this is. This depends on which type of page we are in: a top level page,
  // or a page that is split in sub-pages.
  // If it's a subsection/subpage, then there's no scroll to do, so disable it.
  var selector = 'div.sphinxsidebar > ul.current > li.current > ul.current > li.current';

  if ($(selector).length == 0) {
    $('body').scrollspy({ target: 'div.sphinxsidebar > ul.current > li.current', offset: 10 });
  }

  // Change class from current to active for navbar pills
  $("div.sphinxsidebar a.reference.current").removeClass("current").addClass("active")

  // Grid layout Style
  $(".sphinxsidebar > ul").addClass('nav flex-column nav-pills')
    .find('li').addClass('nav-item').end()
    .find('a.reference').addClass('nav-link').end()

  $(".related").addClass("col-md-12");
  $(".footer").addClass("col-md-12");

  // Tables
  $("table.docutils").addClass("table table-striped").removeClass("docutils")
    .find("thead")
    .addClass("table-dark")

  // Admonition
  $(".admonition").addClass("alert").removeClass("admonition")
    .filter(".hint").removeClass("hint").addClass("alert-info").children('p.admonition-title').prepend('<div class="icon"></div>').end().end()
    .filter(".note").removeClass("note").addClass("alert-primary").children('p.admonition-title').prepend('<div class="icon"></div>').end().end()
    .filter(".warning").removeClass("warning").addClass("alert-warning").children('p.admonition-title').prepend('<div class="icon"></div>').end().end()
    .filter(".tip").removeClass("tip").addClass("alert-info").children('p.admonition-title').prepend('<div class="icon"></div>').end().end()
    .filter(".important").removeClass("important").addClass("alert-danger").children('p.admonition-title').prepend('<div class="icon"></div>').end().end()

  // images
  $(".documentwrapper img").addClass("img-fluid");
  // do not set img-fluid on image in tables
  $(".documentwrapper table img").removeClass("img-fluid");

  // Fix embedded ToC (example page)
  $("div.topic > ul").addClass("list-group");
  $("div.topic > ul > li").addClass("list-group-item").addClass("flex-fill").addClass("list-group-item-action");
  $("div.topic").removeClass("topic");

  // Replace permalink unicode emoji by Font Awesome
  $("a.headerlink").html(" <i class=\"fas fa-link\"></i>");

  // Remove the toctree on the frontpage
  // Hiding it is not enough
  $(".toctree-wrapper").remove();

  // Sphinx adds an empty <p></p> before feature tag substitution on tables: remove it
  $("div.feature-tag").prev("p").each(
    function () {
      var p = $(this);
      if(p.text() == "") {
        p.remove();
      }
    }
  );
});

