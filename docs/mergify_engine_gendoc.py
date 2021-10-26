import glob
import os

import docutils
import jinja2
import sphinx.addnodes


def store_action(app, doctree):
    source = doctree["source"]
    if "actions/" in source and not source.endswith("actions/index.rst"):
        title = None
        summary = None
        icon = None
        for child in doctree.traverse():
            if title is None and isinstance(child, docutils.nodes.title):
                title = child[0]
            if isinstance(child, sphinx.addnodes.meta):
                if summary is None and child["name"] == "summary":
                    summary = child["content"]
                if icon is None and child["name"] == "doc:icon":
                    icon = child["content"]

            if summary is not None and title is not None and icon is not None:
                break

        if title is None or summary is None or icon is None:
            raise ValueError(
                f"Missing meta item for {source}. "
                "Make sure the page has a title and that you have summary "
                "and doc:icon set in meta."
            )

        link = os.path.basename(doctree["source"].removesuffix(".rst"))
        app._MERGIFY_ACTIONS.append((title, summary, link, icon))


def render_action_list(app, docname, source):
    if docname == "actions/index":
        with open(os.path.join(app.srcdir, "_templates/action-list.j2"), "r") as t:
            template = jinja2.Template(t.read())
        with open(os.path.join(app.srcdir, "actions/action-list.html"), "w") as l:
            l.write(template.render(actions=sorted(app._MERGIFY_ACTIONS)))


def order_files(app, env, docnames):
    # Make sure we process all actions files to have them generated in the
    # action-list
    for doc in env.found_docs:
        if doc.startswith("actions/") and doc not in docnames:
            docnames.append(doc)
    # Make sure actions/index is rendered last so we have time to generate the
    # HTML file
    try:
        docnames.remove("actions/index")
    except ValueError:
        pass
    else:
        docnames.append("actions/index")


def write_redirect(app, pagename, templatename, context, doctree):
    url = f"https://docs.mergify.com/{pagename}/"
    os.makedirs(os.path.join(app.outdir, pagename), exist_ok=True)
    with open(os.path.join(app.outdir, pagename + ".html"), "w") as f:
        f.write(f"""<head>
  <meta http-equiv="refresh" content="0; URL={url}">
  <link rel="canonical" href="{url}">
</head>
""")


def setup(app):
    app._MERGIFY_ACTIONS = []
    app.connect("source-read", render_action_list)
    app.connect("doctree-read", store_action)
    app.connect("env-before-read-docs", order_files)

    app.connect("html-page-context", write_redirect)
