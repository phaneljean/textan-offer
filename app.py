
# --- SendGrid Inbound Parse: TC File Check via Email ---

@app.route("/v1/inbound-parse", methods=["POST", "GET"])
def inbound_parse_firewall():
    """SendGrid Inbound Parse webhook for TC File Check email intake.
    Users forward TREC 20-19 PDFs to tc@check.txtanoffer.com and receive
    an audit report by reply."""
    
    if request.method == "GET":
        return "TC File Check Email Intake Active - POST PDFs to tc@check.txtanoffer.com", 200

    from tc_check_email import (
        extract_sender_email, extract_pdf_attachments,
        format_reply_body, format_reply_html, subject_line,
        format_no_pdf_reply, format_no_pdf_html,
        format_unreadable_reply, format_unreadable_html
    )
    from tc_audit import check_tc_file
    from tc_gate import get_tc_check_count
    from integrations import send_html_email

    from_raw = request.form.get("from", "")
    sender_email = extract_sender_email(from_raw)
    pdfs = extract_pdf_attachments(request.files, request.form)

    print(f"[TC_CHECK_EMAIL] from={from_raw} sender={sender_email} pdf_count={len(pdfs)}")

    # No PDF attached
    if not pdfs:
        if sender_email:
            send_html_email(
                sender_email,
                "TC File Check: No PDF found",
                format_no_pdf_reply(),
                format_no_pdf_html()
            )
        return "OK", 200

    # Process the first PDF (ignore extras)
    pdf_file = pdfs[0]
    try:
        result = check_tc_file(pdf_file)
    except Exception as e:
        print(f"[TC_CHECK_EMAIL] check_tc_file error: {e}")
        import traceback; traceback.print_exc()
        if sender_email:
            send_html_email(
                sender_email,
                "TC File Check: Couldn't read PDF",
                format_unreadable_reply(),
                format_unreadable_html()
            )
        return "OK", 200

    # Get check count for this sender (for streak messaging)
    check_count = get_tc_check_count(sender_email) if sender_email else 0

    # Send reply with audit results
    if sender_email:
        send_html_email(
            sender_email,
            subject_line(result),
            format_reply_body(result, check_count),
            format_reply_html(result, check_count)
        )
    
    track_event("tc_check_email", sender_email, {
        "recognized": result.get("recognized", False),
        "blocker_count": len([i for i in result.get("issues", []) if i.get("severity") == "blocker"])
    })

    return "OK", 200
