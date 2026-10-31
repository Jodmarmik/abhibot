import html
import re
from html.parser import HTMLParser
from string import Formatter
from urllib.parse import urlsplit

TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "tg-spoiler", "blockquote"}
FIELDS = {"name", "first_name", "username", "id", "mention"}


class TelegramHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag not in TAGS:
            raise ValueError(f"Unsupported HTML tag: {tag}")
        attributes = dict(attrs)
        if len(attributes) != len(attrs):
            raise ValueError("Duplicate HTML attributes.")
        if tag == "a":
            if set(attributes) != {"href"}:
                raise ValueError("Links require exactly one href attribute.")
            href = attributes["href"] or ""
            if any(c in href for c in "{}"):
                raise ValueError("Placeholders cannot be used in HTML attributes.")
            if urlsplit(href).scheme not in ("https", "http", "tg"):
                raise ValueError("Links must use https, http, or tg.")
        elif attrs:
            raise ValueError("HTML attributes are only supported on links.")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise ValueError("Welcome HTML tags must be correctly nested.")

    def handle_startendtag(self, tag, attrs):
        raise ValueError("Self-closing HTML tags are unsupported.")

    def handle_comment(self, data):
        raise ValueError("HTML comments are unsupported.")


def validate_template(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 700:
        raise ValueError("Welcome text must contain 1–700 characters.")
    for _, field, spec, conversion in Formatter().parse(text):
        if field is not None and (field not in FIELDS or spec or conversion):
            raise ValueError("Use only {name}, {first_name}, {username}, {id}, and {mention}.")
    if re.search(r"<(?!(?:/?[a-zA-Z][^<>]*>))", text):
        raise ValueError("Escape literal < characters as &lt;.")
    if re.search(r"&(?!amp;|lt;|gt;|quot;|#\d+;|#x[0-9a-fA-F]+;)", text):
        raise ValueError("Escape literal ampersands as &amp; and use supported HTML entities.")
    parser = TelegramHTML()
    parser.feed(text)
    parser.close()
    if parser.stack:
        raise ValueError("Welcome HTML has unclosed tags.")
    return text


def render(text, user):
    validate_template(text)

    def escaped(x):
        return html.escape(str(x), quote=True)

    first = escaped(user.first_name or "")
    return text.format(
        name=escaped(user.full_name),
        first_name=first,
        username=escaped("@" + user.username if user.username else user.first_name or ""),
        id=escaped(user.id),
        mention=f'<a href="tg://user?id={int(user.id)}">{first}</a>',
    )
