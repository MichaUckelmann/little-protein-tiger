"""Shared bits for the showcase builders: the public base URL and page <head> meta."""

# Where these pages are served once GitHub Pages is enabled from /docs.
# Social crawlers need ABSOLUTE urls, so og:image cannot be a relative path.
SITE = "https://michauckelmann.github.io/little-protein-tiger/showcase"

FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    'family=Newsreader:opsz,wght@6..72,400;6..72,500&'
    'family=IBM+Plex+Sans:wght@400;500;600;700&'
    'family=IBM+Plex+Mono:wght@400;500&display=swap">'
)


def head(title: str, description: str, page: str, og: str) -> str:
    """<title> plus the OpenGraph/Twitter block a pasted link needs to render a card."""
    url = f"{SITE}/{page}"
    return f"""<title>{title}</title>
<meta name="description" content="{description}">
<meta name="color-scheme" content="light dark">
<meta property="og:type" content="article">
<meta property="og:site_name" content="Little Protein Tiger">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:url" content="{url}">
<meta property="og:image" content="{SITE}/assets/og_{og}.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="{description}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title}">
<meta name="twitter:description" content="{description}">
<meta name="twitter:image" content="{SITE}/assets/og_{og}.png">
<link rel="canonical" href="{url}">
{FONTS}"""
