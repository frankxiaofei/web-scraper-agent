"""发票 PDF 生成（Commercial C2 C2-11）。"""

from __future__ import annotations

from io import BytesIO

from src.billing.models import Invoice, Tenant


def render_invoice_pdf(invoice: Invoice, tenant: Tenant | None = None) -> bytes:
    """生成简易 PDF 发票（纯文本流，无需额外依赖）。"""
    lines = [
        "WebScraperAgent Invoice",
        f"Number: {invoice.number}",
        f"Status: {invoice.status}",
        f"Amount (CNY): {invoice.amount_cny}",
        f"Tax (CNY): {invoice.tax_amount_cny}",
        f"Period: {invoice.period_start} — {invoice.period_end}",
        f"Tenant: {tenant.name if tenant else invoice.tenant_id}",
    ]
    if invoice.buyer_name:
        lines.append(f"Buyer: {invoice.buyer_name}")
    if invoice.buyer_tax_id:
        lines.append(f"Tax ID: {invoice.buyer_tax_id}")
    if invoice.paid_at:
        lines.append(f"Paid at: {invoice.paid_at.isoformat()}")

    text = "\n".join(lines)
    return _minimal_pdf(text)


def _minimal_pdf(text: str) -> bytes:
    """最小 PDF 生成（单页文本）。"""
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = f"BT /F1 12 Tf 50 750 Td ({safe}) Tj ET"
    objects = [
        b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n",
        b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n",
        b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources<< /Font<< /F1 5 0 R >> >> >>endobj\n",
        f"4 0 obj<< /Length {len(content)} >>stream\n{content}\nendstream endobj\n".encode(),
        b"5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj\n",
    ]
    buf = BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(buf.tell())
        buf.write(obj)
    xref_pos = buf.tell()
    buf.write(f"xref\n0 {len(offsets)}\n".encode())
    buf.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        buf.write(f"{off:010d} 00000 n \n".encode())
    buf.write(
        f"trailer<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF".encode()
    )
    return buf.getvalue()
