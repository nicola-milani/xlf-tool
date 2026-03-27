"""
XLIFF 1.2 and 2.0 parser / writer.

Two output modes are supported:
  OutputMode.TARGET  — standard XLIFF: translated text is written as a <target>
                       element alongside the original <source>.
  OutputMode.REPLACE — Articulate Storyline style: <source> text is replaced
                       in-place; no <target> element is added.

Articulate:DocumentState units (rich text with inline <pc> spans) are handled
by extracting each leaf <pc> as a separate Segment, preserving the styling
structure (<originalData>, <ph>, outer <pc> wrappers) intact.
"""
import copy
import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

NS_12 = "urn:oasis:names:tc:xliff:document:1.2"
NS_20 = "urn:oasis:names:tc:xliff:document:2.0"


class OutputMode(Enum):
    TARGET  = "target"   # add <target> alongside <source>  (standard XLIFF)
    REPLACE = "replace"  # replace <source> text in-place   (Articulate Storyline)


@dataclass
class Segment:
    unit_id:   str
    source:    str
    target:    str = ""
    note:      str = ""
    unit_type: str = ""  # e.g. "Articulate:PlainText", "Articulate:DocumentState"
    pc_id:     str = ""  # non-empty for DocumentState leaf-<pc> segments


class XlfParser:
    def __init__(self):
        self.version:     str = "1.2"
        self.source_lang: str = ""
        self.target_lang: str = ""
        self.original:    str = ""
        self.segments:        List[Segment] = []
        self._tree:           Optional[ET.ElementTree] = None
        self._root:           Optional[ET.Element]     = None
        self._ns:             str = ""
        self._original_bytes: bytes = b""

    # ── helpers ─────────────────────────────────────────────────────────────

    def _q(self, tag: str) -> str:
        """Clark-notation qualified tag name."""
        return f"{{{self._ns}}}{tag}" if self._ns else tag

    def _text(self, el: Optional[ET.Element]) -> str:
        return (el.text or "").strip() if el is not None else ""

    def _iter_pc_leaves(self, el: ET.Element):
        """
        Recursively yield leaf <pc> elements that directly contain text.
        Outer <pc> wrappers (e.g. block_0) that only contain other <pc>/<ph>
        children are skipped; only the innermost text-bearing spans are returned.
        """
        tag_pc = self._q("pc")
        for child in el:
            if child.tag != tag_pc:
                continue
            inner_pcs = [c for c in child if c.tag == tag_pc]
            if inner_pcs:
                yield from self._iter_pc_leaves(child)
            elif child.text:
                yield child

    # ── load ────────────────────────────────────────────────────────────────

    def load(self, filepath: str) -> None:
        with open(filepath, "rb") as fh:
            raw = fh.read()
        # Articulate Storyline export bug: closing tags are written as
        # </un{id}it> instead of </unit>.  Fix before parsing.
        self._original_bytes = re.sub(rb"</un([A-Za-z0-9]+)it>", b"</unit>", raw)
        self._tree = ET.parse(io.BytesIO(self._original_bytes))
        self._root = self._tree.getroot()

        root_tag = self._root.tag
        if "{" in root_tag:
            self._ns = root_tag[1:root_tag.index("}")]
            self.version = "2.0" if self._ns == NS_20 else "1.2"
        else:
            self._ns = ""
            self.version = self._root.get("version", "1.2")

        self.segments = []
        if self.version == "1.2":
            self._parse_12()
        else:
            self._parse_20()

    def _parse_12(self) -> None:
        for file_el in self._root.iter(self._q("file")):
            self.source_lang = file_el.get("source-language", "")
            self.target_lang = file_el.get("target-language", "")
            self.original    = file_el.get("original", "")
            break
        for tu in self._root.iter(self._q("trans-unit")):
            uid    = tu.get("id", "")
            source = self._text(tu.find(self._q("source")))
            target = self._text(tu.find(self._q("target")))
            note   = self._text(tu.find(self._q("note")))
            if source:
                self.segments.append(Segment(unit_id=uid, source=source, target=target, note=note))

    def _parse_20(self) -> None:
        self.source_lang = self._root.get("srcLang", "")
        self.target_lang = self._root.get("trgLang", "")
        for file_el in self._root.iter(self._q("file")):
            self.original = file_el.get("original", "")
            break

        for unit in self._root.iter(self._q("unit")):
            uid       = unit.get("id", "")
            unit_type = unit.get("type", "")

            note = ""
            notes_el = unit.find(self._q("notes"))
            if notes_el is not None:
                note_el = notes_el.find(self._q("note"))
                if note_el is not None:
                    note = (note_el.text or "").strip()

            for seg in unit.iter(self._q("segment")):
                src_el = seg.find(self._q("source"))
                if src_el is None:
                    continue

                if unit_type == "Articulate:DocumentState":
                    # One Segment per leaf <pc> — preserves per-span styling
                    for pc in self._iter_pc_leaves(src_el):
                        text = (pc.text or "").strip()
                        if not text:
                            continue
                        # Read existing target text for the same pc id if present
                        tgt_text = ""
                        tgt_el = seg.find(self._q("target"))
                        if tgt_el is not None:
                            for tgt_pc in self._iter_pc_leaves(tgt_el):
                                if tgt_pc.get("id") == pc.get("id"):
                                    tgt_text = (tgt_pc.text or "").strip()
                                    break
                        self.segments.append(Segment(
                            unit_id=uid, source=text, target=tgt_text,
                            note=note, unit_type=unit_type, pc_id=pc.get("id", ""),
                        ))
                else:
                    # PlainText / unknown — source text is a plain string
                    source = self._text(src_el)
                    if not source:
                        continue
                    target = self._text(seg.find(self._q("target")))
                    self.segments.append(Segment(
                        unit_id=uid, source=source, target=target,
                        note=note, unit_type=unit_type,
                    ))

    # ── update ──────────────────────────────────────────────────────────────

    def update_target(self, unit_id: str, new_target: str, pc_id: str = "") -> None:
        """
        Store a translation in the in-memory segment list.
        The XML tree is written only when save() is called.

        Parameters
        ----------
        unit_id    : the unit's id attribute
        new_target : translated text
        pc_id      : for Articulate:DocumentState segments, the id of the
                     leaf <pc> element; empty string for all other units
        """
        for seg in self.segments:
            if seg.unit_id == unit_id and seg.pc_id == pc_id:
                seg.target = new_target
                break

    # ── language ────────────────────────────────────────────────────────────

    def set_target_language(self, lang: str) -> None:
        self.target_lang = lang
        if self.version == "1.2":
            for file_el in self._root.iter(self._q("file")):
                file_el.set("target-language", lang)
        else:
            self._root.set("trgLang", lang)

    # ── in-memory XML rendering (for diff view) ──────────────────────────────

    def get_source_xml(self) -> str:
        """Return the original (untranslated) XML as an indented string."""
        tree = ET.parse(io.BytesIO(self._original_bytes))
        root = tree.getroot()
        if self._ns:
            ET.register_namespace("", self._ns)
        try:
            ET.indent(root, space="  ")
        except AttributeError:
            pass
        buf = io.BytesIO()
        tree.write(buf, encoding="UTF-8", xml_declaration=True)
        return buf.getvalue().decode("utf-8")

    def get_translated_xml(self, mode: OutputMode) -> str:
        """
        Return the translated XML as an indented string without modifying
        the live tree (uses a deep copy).
        """
        tree_copy = copy.deepcopy(self._tree)
        orig_tree, orig_root = self._tree, self._root
        self._tree = tree_copy
        self._root = tree_copy.getroot()
        try:
            translations = {
                (s.unit_id, s.pc_id): s.target
                for s in self.segments
                if s.target
            }
            if self.version == "1.2":
                self._apply_12(translations, mode)
            else:
                self._apply_20(translations, mode)
            if self._ns:
                ET.register_namespace("", self._ns)
            try:
                ET.indent(self._root, space="  ")
            except AttributeError:
                pass
            buf = io.BytesIO()
            self._tree.write(buf, encoding="UTF-8", xml_declaration=True)
            return buf.getvalue().decode("utf-8")
        finally:
            self._tree = orig_tree
            self._root = orig_root

    # ── save ────────────────────────────────────────────────────────────────

    def save(self, filepath: str, mode: OutputMode = OutputMode.TARGET) -> None:
        """
        Write the translated file.

        Parameters
        ----------
        filepath : output path
        mode     : OutputMode.TARGET  — <target> element added next to <source>
                   OutputMode.REPLACE — <source> replaced in-place (Articulate)
        """
        translations = {
            (s.unit_id, s.pc_id): s.target
            for s in self.segments
            if s.target
        }

        if self.version == "1.2":
            self._apply_12(translations, mode)
        else:
            self._apply_20(translations, mode)

        if self._ns:
            ET.register_namespace("", self._ns)

        # Indent only in TARGET mode — Articulate re-imports may be
        # sensitive to added whitespace inside text nodes.
        if mode == OutputMode.TARGET:
            try:
                ET.indent(self._root, space="  ")
            except AttributeError:
                pass

        self._tree.write(filepath, encoding="UTF-8", xml_declaration=True)

    # ── apply 1.2 ───────────────────────────────────────────────────────────

    def _apply_12(self, translations: dict, mode: OutputMode) -> None:
        for tu in self._root.iter(self._q("trans-unit")):
            uid        = tu.get("id", "")
            translated = translations.get((uid, ""), "")
            if not translated:
                continue

            if mode == OutputMode.REPLACE:
                src_el = tu.find(self._q("source"))
                if src_el is not None:
                    src_el.text = translated
                tgt_el = tu.find(self._q("target"))
                if tgt_el is not None:
                    tu.remove(tgt_el)
            else:
                tgt_el = tu.find(self._q("target"))
                if tgt_el is None:
                    tgt_el = ET.SubElement(tu, self._q("target"))
                tgt_el.text = translated

    # ── apply 2.0 ───────────────────────────────────────────────────────────

    def _apply_20(self, translations: dict, mode: OutputMode) -> None:
        for unit in self._root.iter(self._q("unit")):
            uid       = unit.get("id", "")
            unit_type = unit.get("type", "")

            for seg in unit.iter(self._q("segment")):
                src_el = seg.find(self._q("source"))
                if src_el is None:
                    continue

                if unit_type == "Articulate:DocumentState":
                    self._apply_20_doc_state(seg, src_el, uid, translations, mode)
                else:
                    translated = translations.get((uid, ""), "")
                    if not translated:
                        continue

                    if mode == OutputMode.REPLACE:
                        src_el.text = translated
                        tgt_el = seg.find(self._q("target"))
                        if tgt_el is not None:
                            seg.remove(tgt_el)
                    else:
                        tgt_el = seg.find(self._q("target"))
                        if tgt_el is None:
                            tgt_el = ET.Element(self._q("target"))
                            seg.insert(list(seg).index(src_el) + 1, tgt_el)
                        tgt_el.text = translated

    def _apply_20_doc_state(
        self,
        seg: ET.Element,
        src_el: ET.Element,
        uid: str,
        translations: dict,
        mode: OutputMode,
    ) -> None:
        """Handle Articulate:DocumentState inline-markup segments."""
        if mode == OutputMode.REPLACE:
            # Replace text directly inside each leaf <pc> of the source
            for pc in self._iter_pc_leaves(src_el):
                translated = translations.get((uid, pc.get("id", "")), "")
                if translated:
                    pc.text = translated
            # Remove any stale <target> from a previous translation pass
            tgt_el = seg.find(self._q("target"))
            if tgt_el is not None:
                seg.remove(tgt_el)

        else:
            # Deep-copy the source structure into <target>, then substitute
            # text inside each leaf <pc> with the translation.
            tgt_el = seg.find(self._q("target"))
            if tgt_el is None:
                tgt_el = copy.deepcopy(src_el)
                tgt_el.tag = self._q("target")
                seg.insert(list(seg).index(src_el) + 1, tgt_el)
            for pc in self._iter_pc_leaves(tgt_el):
                translated = translations.get((uid, pc.get("id", "")), "")
                if translated:
                    pc.text = translated


# ── Standalone utility ───────────────────────────────────────────────────────

def indent_file(src_path: str, dst_path: str) -> None:
    """
    Parse *src_path* and write it to *dst_path* with pretty-print indentation.
    Namespace prefixes are preserved.  Works on any XLIFF 1.2 / 2.0 file.
    """
    tree = ET.parse(src_path)
    root = tree.getroot()
    if "{" in root.tag:
        ns = root.tag[1:root.tag.index("}")]
        ET.register_namespace("", ns)
    try:
        ET.indent(root, space="  ")
    except AttributeError:
        pass  # Python < 3.9 — no indentation applied
    tree.write(dst_path, encoding="UTF-8", xml_declaration=True)
