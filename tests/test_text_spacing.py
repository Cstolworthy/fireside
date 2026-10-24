#!/usr/bin/env python3
"""Regression tests for whitespace retained for reading and speech synthesis.

Run from repository root: python3 -m unittest discover -s tests -p 'test_text_spacing.py'
No external packages or network required.
"""
import io
import unittest
import zipfile

from host.textutil import clean_fragment, text_of
from plugins.reader.epub.plugin import parse_epub
from providers._text import chunk_text


class TextSpacingTests(unittest.TestCase):
    def test_epub_paragraphs_not_flattened(self):
        source = '<html><body><p>First paragraph.</p><p>Second paragraph.</p></body></html>'
        self.assertEqual(text_of(source), 'First paragraph.\n\nSecond paragraph.')

    def test_epub_heading_separate_from_paragraph(self):
        self.assertEqual(
            text_of('<h2>Chapter One</h2><p>Once upon a time.</p>'),
            'Chapter One\n\nOnce upon a time.')

    def test_line_break_is_not_a_paragraph(self):
        self.assertEqual(text_of('<p>First line<br/>Second line</p>'),
                         'First line\nSecond line')

    def test_indented_whitespace_and_entities(self):
        src = '<div>\n  <p>  A &amp; B.  </p>\n  <p>  C.  </p>\n</div>'
        self.assertEqual(text_of(src), 'A & B.\n\nC.')

    def test_html_chrome_removed(self):
        src = '<head><title>Invisible</title></head><p>Welcome.</p><p>Search</p><p>Keep me.</p>'
        self.assertEqual(text_of(src), 'Welcome.\n\nKeep me.')

    def test_web_fragment_retains_paragraphs(self):
        src = '<article><p>First.</p><p>Second.</p></article>'
        self.assertEqual(clean_fragment(src), 'First.\n\nSecond.')

    def test_web_fragment_ignores_nav_and_script(self):
        src = '<nav>menu links</nav><p>First.</p><script>bad()</script><p>Second.</p>'
        self.assertEqual(clean_fragment(src), 'First.\n\nSecond.')

    def test_tts_chunking_keeps_boundaries(self):
        text = text_of('<p>First.</p><p>Second.</p>')
        self.assertEqual(chunk_text(text), ['First.\n\nSecond.'])

    def test_epub_import_preserves_paragraphs(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as z:
            z.writestr('META-INF/container.xml',
                       '<container><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
            z.writestr('content.opf',
                       '<package><metadata><dc:title>Example</dc:title></metadata>'
                       '<manifest><item id="x" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
                       '</manifest><spine><itemref idref="x"/></spine></package>')
            z.writestr('chapter.xhtml',
                       '<html><body><h1>Intro</h1><p>One.</p><p>Two.</p></body></html>')
        result = parse_epub(data.getvalue(), 'sample.epub')
        self.assertEqual(result['chapters'][0]['text'], 'Intro\n\nOne.\n\nTwo.')


if __name__ == '__main__':
    unittest.main()
