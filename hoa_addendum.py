"""
hoa_addendum.py — Attach TREC 36-11, Addendum for Property Subject to
Mandatory Membership in a Property Owners Association, when the agent's
text signals the property has an HOA. Unlike the Water Disclosure (always
attached), this one is conditional -- not every property has a mandatory
HOA, so attaching it to every offer would be inaccurate on a legal document.

36-11 (dated 05-04-2026, effective 07/01/2026, "This form replaces TREC
No. 36-10") replaced 36-10. Source: trec.texas.gov/forms/addendum-property-
subject-mandatory-membership-property-owners-association ("Form ID: 36-11,
Effective Date: 07/01/2026"), PDF at /sites/default/files/pdf-forms/36-11.pdf.
Not the "36-11 draft.pdf" on the same page -- that's TREC's redline.

36-10 was a flat form (zero AcroForm fields), so the address went on as a
reportlab overlay. 36-11 ships 17 real fields, so the address now goes into
the form's own address field -- otherwise the empty field would sit on top
of the overlay and an agent typing into it would get the address twice.

Only the property address is filled in -- every substantive section here
(how Subdivision Information gets delivered, the Association transfer-fee
cap, who pays the title company, the HOA's own name/phone) requires facts
this app has no way to collect from a one-line SMS offer string. Guessing
at any of those would be worse than leaving them blank for the agent to
complete by hand -- same principle as the Water Disclosure and buyer/seller
names elsewhere in this app.
"""
import os
import io
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (NameObject, BooleanObject, TextStringObject, DictionaryObject,
                           ArrayObject, FloatObject, StreamObject)
from reportlab.pdfbase.pdfmetrics import stringWidth

from water_disclosure import _full_address

TEMPLATE_PATH = os.environ.get("HOA_ADDENDUM_TEMPLATE_PATH", "36-11.pdf")

# "ADDENDUM TO CONTRACT CONCERNING THE PROPERTY AT ___" blank. Rect-verified
# 2026-10-25 against 36-11.pdf: widget spans x 36-576, top-down y 121.6-134.4,
# sitting on the rule at y 132.5 just above the "(Street Address and City)"
# caption -- and confirmed by filling it and rendering the page. The /T name
# happens to be right this time; several others on this form are not (the
# A.2 days blank is named "copy of the Subdivision Information to the
# Seller", the C fee cap "D DEPOSITS FOR RESERVES..."), left over from
# older 36-x wording, so don't map anything else here by name.
ADDRESS_FIELD = "Street Address and City"


def _address_appearance(annot, text: str) -> StreamObject:
    """Appearance stream for the address field, written by hand.
    pypdf 4.3.1's own generator encodes the value as 2-byte hex against
    this form's single-byte Helvetica, so every character renders with a
    blank glyph in front of it ("1 2 3  M a i n" -- seen in both PyMuPDF
    and macOS Preview renders, 2026-10-25). Drawn the way the field asks:
    /Helv 9 (its /DA), centered (/Q 1) -- same centering the 36-10
    overlay used."""
    x0, y0, x1, y1 = [float(v) for v in annot["/Rect"]]
    w, h = x1 - x0, y1 - y0
    size = 9
    # Latin-1 in, "?" for anything Helvetica's WinAnsi can't show.
    raw = text.encode("cp1252", "replace").decode("cp1252")
    while size > 6 and stringWidth(raw, "Helvetica", size) > w - 4:
        size -= 0.5
    x = max(2, (w - stringWidth(raw, "Helvetica", size)) / 2)
    esc = raw.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = StreamObject()
    stream._data = (f"/Tx BMC q BT /Helv {size:g} Tf 0 g {x:.2f} 3 Td ({esc}) Tj ET Q EMC"
                    ).encode("cp1252")
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    })
    stream.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Form"),
        NameObject("/BBox"): ArrayObject([FloatObject(0), FloatObject(0), FloatObject(w), FloatObject(h)]),
        NameObject("/Resources"): DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/Helv"): font})}),
    })
    return stream


def fill_hoa_addendum(parsed: dict) -> bytes:
    if not os.path.exists(TEMPLATE_PATH):
        raise FileNotFoundError(f"TREC 36-11 template not found at {TEMPLATE_PATH}")

    reader = PdfReader(TEMPLATE_PATH)
    writer = PdfWriter()
    writer.append(reader)

    full_addr = _full_address(parsed)
    for annot_ref in writer.pages[0].get("/Annots", []):
        annot = annot_ref.get_object()
        if annot.get("/T") == ADDRESS_FIELD:
            annot[NameObject("/V")] = TextStringObject(full_addr)
            annot[NameObject("/AP")] = DictionaryObject(
                {NameObject("/N"): writer._add_object(_address_appearance(annot, full_addr))})

    acroform = writer._root_object.get("/AcroForm")
    if hasattr(acroform, 'get_object'):
        acroform = acroform.get_object()
    if acroform is not None:
        acroform[NameObject("/NeedAppearances")] = BooleanObject(True)

    # Namespace every field before pdf_filler.py merges this in, for the
    # same reason as financing_addendum.py's FA_ prefix: PdfWriter.append()
    # turns same-named fields from different source PDFs into one shared
    # field. 36-11 shares "Buyer" (20-19's 6C(1) survey box, which TC Check
    # reads) and "undefined" with 20-19_2.pdf, plus "Street Address and
    # City" and Signature1-4 with 40-11 -- unprefixed, the Paragraph D
    # "Buyer pays" box here and the 20-19 survey box would toggle together.
    def _namespace_fields(field_refs):
        for field_ref in field_refs:
            field = field_ref.get_object()
            if "/T" in field:
                field[NameObject("/T")] = TextStringObject("HOA_" + str(field["/T"]))
            if "/Kids" in field:
                _namespace_fields(field["/Kids"])

    if acroform is not None and "/Fields" in acroform:
        _namespace_fields(acroform["/Fields"])

    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)
    return buf.getvalue()
