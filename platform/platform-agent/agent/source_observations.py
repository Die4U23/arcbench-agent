"""Coverage of source actually visible in one model context, not quality evidence."""
from __future__ import annotations

import hashlib
import json
import os
import re


READ_TOOLS = frozenset({'read_file', 'read_files', 'list_files', 'search_text',
                        'read_context', 'list_context'})


class SourceObservations:
    def __init__(self):
        self.ranges = {}
        self.entries = set()

    def span(self, path, sha, start, text):
        if not isinstance(text, str) or type(start) is not int:
            return False
        return self.cover(path, sha, start, start + len(text))

    def cover(self, path, sha, start, end):
        if not path or not sha or type(start) is not int or type(end) is not int or not 0 <= start < end:
            return False
        key = (path, sha)
        spans = self.ranges.setdefault(key, [])
        novel = not any(left <= start and right >= end for left, right in spans)
        merged = []
        for left, right in sorted([*spans, (start, end)]):
            if merged and left <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
            else:
                merged.append((left, right))
        self.ranges[key] = merged
        return novel

    def entries_seen(self, entries):
        entries = set(entries)
        novel = bool(entries - self.entries)
        self.entries.update(entries)
        return novel

    def context_range(self, path, sha, start, end, files):
        """Context offsets use raw text; file tools normalize Windows newlines."""
        path = path.removeprefix('application/') if isinstance(path, str) else path
        raw = files.get(path)
        if raw is not None and hashlib.sha256(raw).hexdigest() == sha and type(start) is int and type(end) is int:
            text = raw.decode('utf-8')
            start = len(text[:start].replace('\r\n', '\n').replace('\r', '\n'))
            end = len(text[:end].replace('\r\n', '\n').replace('\r', '\n'))
        return self.cover(path, sha, start, end)

    def file(self, tools, files, path, content, start_line=1):
        path = tools._resolve(path).relative_to(tools.project_dir).as_posix()
        raw = files.get(path)
        if raw is None:
            return False
        text = raw.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
        start = sum(len(line) for line in text.splitlines(keepends=True)[:start_line - 1])
        # Only the real returned prefix counts: exclude truncation markers and
        # requested ranges past EOF, and canonicalize aliases/overlapping reads.
        returned = os.path.commonprefix([text[start:], content.replace('\r\n', '\n').replace('\r', '\n')])
        return self.span(path, hashlib.sha256(raw).hexdigest(), start, returned)

    def observe(self, name, arguments, result, *, tools, files):
        if name == 'read_file':
            return self.file(tools, files, arguments['path'], result, arguments.get('start_line', 1))
        if name == 'list_files':
            return self.entries_seen(('file', path) for path in result.splitlines() if path in files)
        if name == 'search_text':
            novel = False
            for line in result.splitlines():
                match = re.fullmatch(r'(.+?):(\d+): (.*)', line)
                if match and match[1] in files:
                    novel |= self.file(tools, files, match[1], match[3], int(match[2]))
            return novel
        try:
            value = json.loads(result)
        except (ValueError, TypeError):
            return False  # An incomplete structured result cannot establish coverage.
        if name == 'read_files':
            novel = False
            for row in value.get('files', []):
                novel |= self.file(tools, files, row['path'], row['content'])
            return novel
        if name == 'read_context':
            start, content = value.get('start'), value.get('content')
            if type(start) is not int or not isinstance(content, str):
                return False
            return self.context_range(value.get('path'), value.get('source_sha256'),
                                      start, start + len(content), files)
        if name == 'list_context':
            return self.entries_seen(('context', json.dumps(row, sort_keys=True, ensure_ascii=False))
                                     for row in value.get('items', []))
        return False
