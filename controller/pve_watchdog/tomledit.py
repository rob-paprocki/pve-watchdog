"""Small, comment-preserving editor for pve-watchdog's config.toml.

The config has a simple shape: top-level keys, [tables] and [[arrays]] of
tables ([[node]], [[ct]]). These functions change single values or whole
array items as TEXT, so every comment and blank line people wrote stays
where it was. Every function returns new text; callers check it with
tomllib before saving it.
"""
import json
import re
import tomllib

HEADER = re.compile(r"^\s*(\[\[?)\s*([A-Za-z0-9_.-]+)\s*\]\]?\s*(#.*)?$")
KEYLINE = re.compile(r"^(\s*)([A-Za-z0-9_-]+)(\s*=\s*)(.*?)(\s+#.*)?$")


def fmt(value):
    """A Python value as a TOML value (only the types the config uses)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)          # JSON string escapes are valid TOML basic strings
    raise ValueError("unsupported value type: %r" % type(value))


def _blocks(lines):
    """[(kind, name, start, end)]; kind '' = top level, '[' table, '[[' array item."""
    out, start, kind, name = [], 0, "", ""
    for i, line in enumerate(lines):
        m = HEADER.match(line)
        if m:
            out.append((kind, name, start, i))
            kind, name, start = m.group(1), m.group(2), i
    out.append((kind, name, start, len(lines)))
    return out


def _content_end(lines, start, end):
    """Index after the last key line of a block (so inserts go before trailing blanks)."""
    last = start
    for i in range(start, end):
        if KEYLINE.match(lines[i]) or HEADER.match(lines[i]):
            last = i
    return last + 1


def _set_in(lines, start, end, key, value):
    for i in range(start, end):
        m = KEYLINE.match(lines[i])
        if m and m.group(2) == key:
            lines[i] = "%s%s%s%s%s" % (m.group(1), key, m.group(3), fmt(value), m.group(5) or "")
            return lines
    pos = _content_end(lines, start, end)
    lines.insert(pos, "%s = %s" % (key, fmt(value)))
    return lines


def _check(text):
    tomllib.loads(text)
    return text


def set_top(text, key, value):
    lines = text.split("\n")
    _, _, s, e = _blocks(lines)[0]
    return _check("\n".join(_set_in(lines, s, e, key, value)))


def set_in_table(text, table, key, value):
    lines = text.split("\n")
    for kind, name, s, e in _blocks(lines):
        if kind == "[" and name == table:
            return _check("\n".join(_set_in(lines, s, e, key, value)))
    lines += ["", "[%s]" % table, "%s = %s" % (key, fmt(value))]
    return _check("\n".join(lines))


def _find_item(lines, array, match_key, match_val):
    for kind, name, s, e in _blocks(lines):
        if kind != "[[" or name != array:
            continue
        for i in range(s + 1, e):
            m = KEYLINE.match(lines[i])
            if m and m.group(2) == match_key and tomllib.loads("v = " + m.group(4))["v"] == match_val:
                return s, e
    return None


def upsert_item(text, array, match_key, match_val, fields, remove_keys=()):
    """Update the [[array]] item whose match_key == match_val, or append a new one."""
    lines = text.split("\n")
    found = _find_item(lines, array, match_key, match_val)
    if found is None:
        # Append after the last item of the same array (or at the end).
        pos = len(lines)
        for kind, name, s, e in _blocks(lines):
            if kind == "[[" and name == array:
                pos = _content_end(lines, s, e)
        new = ["", "[[%s]]" % array, "%s = %s" % (match_key, fmt(match_val))]
        new += ["%s = %s" % (k, fmt(v)) for k, v in fields.items() if k != match_key]
        lines[pos:pos] = new
        return _check("\n".join(lines))
    s, e = found
    for k, v in fields.items():
        if k == match_key:
            continue
        lines = _set_in(lines, s, e, k, v)
        s, e = _find_item(lines, array, match_key, match_val)
    for k in remove_keys:
        for i in range(s + 1, e):
            m = KEYLINE.match(lines[i])
            if m and m.group(2) == k:
                del lines[i]
                break
        s, e = _find_item(lines, array, match_key, match_val)
    return _check("\n".join(lines))


def delete_item(text, array, match_key, match_val):
    lines = text.split("\n")
    found = _find_item(lines, array, match_key, match_val)
    if found is None:
        raise KeyError("no [[%s]] with %s = %r" % (array, match_key, match_val))
    s, _ = found
    e = _content_end(lines, s, found[1])
    while e < len(lines) and not lines[e].strip():
        e += 1
    del lines[s:e]
    return _check("\n".join(lines))
