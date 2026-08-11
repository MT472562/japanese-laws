import importlib.util
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("sync_laws", Path(__file__).parents[1] / "scripts" / "sync_laws.py")
sync_laws = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(sync_laws)


class MarkdownTests(unittest.TestCase):
    def test_bulk_xml_conversion(self):
        xml = '''<Law Era="Reiwa" Year="01" Num="1" LawType="Act" PromulgateMonth="1" PromulgateDay="2"><LawNum>令和元年法律第一号</LawNum><LawBody><LawTitle>試験法</LawTitle><MainProvision><Article><ArticleTitle>第一条</ArticleTitle><Paragraph><ParagraphSentence><Sentence>本文です。</Sentence></ParagraphSentence></Paragraph></Article></MainProvision></LawBody></Law>'''.encode()
        payload = sync_laws.payload_from_bulk_xml(xml, "501AC0000000001_20190102_000000000000000")
        self.assertEqual(payload["law_info"]["promulgation_date"], "2019-01-02")
        md = sync_laws.law_to_markdown(payload)
        self.assertIn("# 試験法", md)
        self.assertIn("### 第一条", md)
        self.assertIn("本文です。", md)

    def test_renders_metadata_headings_and_sentences(self):
        payload = {
            "law_info": {"law_id": "TEST1", "law_num": "令和元年法律第一号", "law_type": "Act", "promulgation_date": "2019-01-01"},
            "revision_info": {"law_revision_id": "TEST1_REV1", "law_title": "試験法", "amendment_enforcement_date": "2019-02-01"},
            "law_full_text": {"tag": "Law", "children": [
                {"tag": "LawBody", "children": [
                    {"tag": "LawTitle", "children": ["試験法"]},
                    {"tag": "TOC", "children": [{"tag": "ArticleTitle", "children": ["第一条"]}]},
                    {"tag": "MainProvision", "children": [
                        {"tag": "Article", "children": [
                            {"tag": "ArticleTitle", "children": ["第一条"]},
                            {"tag": "Paragraph", "children": [
                                {"tag": "ParagraphSentence", "children": [{"tag": "Sentence", "children": ["これは　本文です。"]}]}
                            ]}
                        ]}
                    ]}
                ]}
            ]}
        }
        md = sync_laws.law_to_markdown(payload)
        self.assertIn("# 試験法", md)
        self.assertEqual(md.count("### 第一条"), 1)
        self.assertIn("これは 本文です。", md)
        self.assertIn("law_revision_id: \"TEST1_REV1\"", md)

    def test_index_is_deterministic(self):
        entries = {
            "B": {"title": "乙法", "law_num": "第二号", "law_type": "Act"},
            "A": {"title": "甲法", "law_num": "第一号", "law_type": "Act"},
        }
        index = sync_laws.build_index(entries)
        self.assertEqual(index, sync_laws.build_index(dict(reversed(list(entries.items())))))
        self.assertIn("[甲法](laws/Act/A.md)", index)
        self.assertIn("[乙法](laws/Act/B.md)", index)


if __name__ == "__main__":
    unittest.main()
