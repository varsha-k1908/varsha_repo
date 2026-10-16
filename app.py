"""Fertility Clinic Follow-up Manager — FastAPI app + staff dashboard."""
import json
import os
from datetime import date, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models import (AuditLog, Base, FollowUp, MessageLog, Patient, engine,
                    generate_id, log_audit, log_message, utcnow)
import reminders
from reminders import (CLINIC_NAME, run_daily_reminders, run_retry_escalation,
                       send_reminder, send_whatsapp, twilio_configured)
from replies import extract_date, looks_like_date, parse_reply

load_dotenv()

Base.metadata.create_all(engine)

app = FastAPI(title="Fertility Clinic Follow-up Manager")
templates = Jinja2Templates(directory="templates")

ACTIVE = ("pending", "reminded")


# ------------------------------------------------------------------ schemas

class IncomingMessage(BaseModel):
    phone: str
    message: str
    channel: str = "whatsapp"


class FollowUpCreate(BaseModel):
    patient_phone: str
    patient_name: Optional[str] = None
    ftype: str = "consultation"
    due_date: str
    language: str = "en"
    notes: Optional[str] = None


class RescheduleBody(BaseModel):
    new_date: str


class PatientCreate(BaseModel):
    name: str
    phone: str
    language: str = "en"


# ------------------------------------------------------------------ helpers

def get_or_create_patient(session, phone, name=None, language="en"):
    patient = session.query(Patient).filter(Patient.phone == phone).first()
    if not patient:
        patient = Patient(id=generate_id("pat"), phone=phone,
                          name=name or "Patient", language=language)
        session.add(patient)
        session.commit()
        session.refresh(patient)
    return patient


def _serialize(fu, today=None):
    today = today or date.today()
    overdue = 0
    try:
        overdue = (today - date.fromisoformat(fu.due_date)).days
    except (ValueError, TypeError):
        pass
    return {
        "id": fu.id,
        "name": fu.patient_name,
        "phone": fu.patient_phone,
        "ftype": fu.ftype,
        "due_date": fu.due_date,
        "status": fu.status,
        "attempts": fu.attempts,
        "awaiting_date": bool(fu.awaiting_date),
        "overdue_days": overdue if overdue > 0 else 0,
        "updated_at": fu.updated_at.isoformat() if fu.updated_at else None,
    }


def apply_reschedule(session, fu, new_date):
    fu.due_date = new_date
    fu.status = "pending"
    fu.attempts = 0
    fu.last_reminded_at = None
    fu.awaiting_date = False
    fu.updated_at = utcnow()
    session.commit()
    log_audit(session, "reschedule", fu.patient_phone,
              f"Rescheduled {fu.ftype} to {new_date}", decided_by="patient")


def finalize_reply(session, phone, reply, action):
    log_message(session, phone, "outbound", "whatsapp", reply, action)
    send_whatsapp(phone, reply)


# ------------------------------------------------------- reply orchestration

def handle_incoming_message(phone, body, channel="whatsapp"):
    """Process an inbound patient message end to end (rule-based)."""
    with Session(engine) as session:
        patient = get_or_create_patient(session, phone)
        patient.last_contact = utcnow()
        session.commit()
        log_message(session, phone, "inbound", channel, body, "received")

        # opted-out patients only get re-enabled by START
        if patient.opted_out:
            if body.strip().lower().startswith("start"):
                patient.opted_out = False
                session.commit()
                finalize_reply(session, phone, "Welcome back! Reminders resumed.", "opt_in")
                return {"status": "opt_in"}
            return {"status": "opted_out"}

        parsed = parse_reply(body)
        action = parsed["action"]
        new_date = parsed["date"]

        active = session.query(FollowUp).filter(
            FollowUp.patient_phone == phone,
            FollowUp.status.in_(ACTIVE),
        ).order_by(FollowUp.due_date.asc()).all()

        # a follow-up is waiting for the patient to supply a new date
        awaiting = [f for f in active if f.awaiting_date]
        if awaiting and action in ("reschedule", "unknown"):
            fu = awaiting[0]
            d = new_date or (extract_date(body) if looks_like_date(body) else None)
            if d:
                apply_reschedule(session, fu, d)
                reply = (f"Thanks {patient.name}. Your "
                         f"{fu.ftype.replace('_', ' ')} follow-up is now on {d}. "
                         f"We'll remind you then.")
                finalize_reply(session, phone, reply, "reschedule")
                return {"status": "rescheduled", "date": d}
            finalize_reply(session, phone,
                           "Please reply with the new date, e.g. '15 Oct' or 'tomorrow'.",
                           "awaiting_date")
            return {"status": "awaiting_date"}

        if action == "confirm":
            if not active:
                finalize_reply(session, phone,
                               "We couldn't find a pending follow-up. Please contact the clinic.",
                               "confirm_notfound")
                return {"status": "not_found"}
            fu = active[0]
            fu.status = "confirmed"
            fu.updated_at = utcnow()
            session.commit()
            log_audit(session, "confirm", phone,
                      f"Confirmed {fu.ftype} due {fu.due_date}", decided_by="patient")
            reply = (f"Thank you {patient.name}! Your {fu.ftype.replace('_', ' ')} "
                     f"follow-up on {fu.due_date} is confirmed.")
            finalize_reply(session, phone, reply, "confirm")
            return {"status": "confirmed"}

        if action == "reschedule":
            if not active:
                finalize_reply(session, phone,
                               "We couldn't find a pending follow-up to reschedule.",
                               "reschedule_notfound")
                return {"status": "not_found"}
            fu = active[0]
            if new_date:
                apply_reschedule(session, fu, new_date)
                reply = (f"No problem {patient.name}. Your follow-up has been moved to "
                         f"{new_date}. We'll remind you then.")
                finalize_reply(session, phone, reply, "reschedule")
                return {"status": "rescheduled", "date": new_date}
            fu.awaiting_date = True
            session.commit()
            finalize_reply(session, phone,
                           "Sure - please reply with the new date, e.g. '15 Oct' or 'tomorrow'.",
                           "awaiting_date")
            return {"status": "awaiting_date"}

        if action == "opt_out":
            patient.opted_out = True
            session.commit()
            log_audit(session, "opt_out", phone, "Patient opted out", decided_by="patient")
            finalize_reply(session, phone,
                           "You've been opted out of reminders. Reply START to resume.",
                           "opt_out")
            return {"status": "opt_out"}

        finalize_reply(session, phone,
                       "Thanks for your message. Reply CONFIRM to confirm your follow-up "
                       "or RESCHEDULE to change the date. For anything else, please call the clinic.",
                       "unknown")
        return {"status": "unknown"}


# ------------------------------------------------------------------ web UI

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request, name="dashboard.html", context={"clinic": CLINIC_NAME})


# ------------------------------------------------------------------ endpoints

@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    form = await request.form()
    frm = form.get("From", "")
    body = form.get("Body", "")
    if not frm or not body:
        raise HTTPException(status_code=400, detail="Missing From or Body")
    phone = frm.replace("whatsapp:", "").strip()
    background_tasks.add_task(handle_incoming_message, phone, body, "whatsapp")
    return {"status": "received"}


@app.post("/message")
def simulate(data: IncomingMessage, background_tasks: BackgroundTasks):
    """Simulator endpoint - test without a real WhatsApp round-trip."""
    background_tasks.add_task(handle_incoming_message,
                              data.phone, data.message, data.channel)
    return {"status": "processing"}


@app.get("/followups/today")
def followups_today():
    today = date.today()
    with Session(engine) as session:
        items = session.query(FollowUp).filter(
            FollowUp.status.in_(ACTIVE),
            FollowUp.due_date <= today.isoformat(),
        ).order_by(FollowUp.due_date.asc()).all()
        return [_serialize(fu, today) for fu in items]


@app.get("/followups/upcoming")
def followups_upcoming():
    today = date.today().isoformat()
    with Session(engine) as session:
        items = session.query(FollowUp).filter(
            FollowUp.status.in_(ACTIVE),
            FollowUp.due_date > today,
        ).order_by(FollowUp.due_date.asc()).all()
        return [_serialize(fu) for fu in items]


@app.get("/followups")
def list_followups(status: Optional[str] = None):
    with Session(engine) as session:
        q = session.query(FollowUp)
        if status:
            q = q.filter(FollowUp.status == status)
        return [_serialize(fu) for fu in q.order_by(FollowUp.due_date.asc()).all()]


@app.get("/patients")
def list_patients():
    with Session(engine) as session:
        patients = session.query(Patient).order_by(Patient.name.asc()).all()
        return [
            {"id": p.id, "name": p.name, "phone": p.phone,
             "language": p.language, "opted_out": bool(p.opted_out),
             "last_contact": p.last_contact.isoformat() if p.last_contact else None}
            for p in patients
        ]


@app.post("/patients")
def create_patient(data: PatientCreate):
    with Session(engine) as session:
        patient = get_or_create_patient(session, data.phone, data.name, data.language)
        patient.name = data.name or patient.name
        patient.language = data.language
        session.commit()
        log_audit(session, "create_patient", data.phone,
                  f"Added patient {data.name}", decided_by="staff")
        return {"id": patient.id, "name": patient.name, "phone": patient.phone}


@app.post("/followups")
def create_followup(data: FollowUpCreate):
    with Session(engine) as session:
        patient = get_or_create_patient(session, data.patient_phone,
                                        data.patient_name, data.language)
        if data.patient_name and patient.name in (None, "Patient"):
            patient.name = data.patient_name
        fu = FollowUp(id=generate_id("fu"), patient_phone=data.patient_phone,
                      patient_name=data.patient_name or patient.name,
                      ftype=data.ftype, due_date=data.due_date, notes=data.notes)
        session.add(fu)
        session.commit()
        log_audit(session, "create", data.patient_phone,
                  f"Created {data.ftype} due {data.due_date}")
        return {"id": fu.id}


@app.post("/followups/{fid}/complete")
def complete_followup(fid: str):
    with Session(engine) as session:
        fu = session.query(FollowUp).filter(FollowUp.id == fid).first()
        if not fu:
            raise HTTPException(status_code=404, detail="Follow-up not found")
        fu.status = "completed"
        fu.updated_at = utcnow()
        session.commit()
        log_audit(session, "complete", fu.patient_phone,
                  f"Marked {fu.ftype} complete")
        return {"status": "completed"}


@app.post("/followups/{fid}/reschedule")
def reschedule_followup(fid: str, data: RescheduleBody):
    with Session(engine) as session:
        fu = session.query(FollowUp).filter(FollowUp.id == fid).first()
        if not fu:
            raise HTTPException(status_code=404, detail="Follow-up not found")
        apply_reschedule(session, fu, data.new_date)
        return {"status": "rescheduled", "new_date": data.new_date}


@app.post("/followups/{fid}/notify")
def notify_followup(fid: str):
    """Send a WhatsApp reminder for a single follow-up right now."""
    with Session(engine) as session:
        fu = session.query(FollowUp).filter(FollowUp.id == fid).first()
        if not fu:
            raise HTTPException(status_code=404, detail="Follow-up not found")
        patient = session.query(Patient).filter(
            Patient.phone == fu.patient_phone).first()
        ok = send_reminder(session, fu, patient)
        if fu.status == "pending":
            fu.status = "reminded"
            fu.attempts = max(1, fu.attempts or 0)
            fu.last_reminded_at = utcnow()
            fu.updated_at = utcnow()
            session.commit()
        log_audit(session, "reminder", fu.patient_phone,
                  f"Manual reminder for {fu.ftype} due {fu.due_date}",
                  decided_by="staff")
        return {"status": "sent" if ok else "failed", "delivered": ok,
                "phone": fu.patient_phone}


@app.get("/stats/weekly")
def weekly_stats():
    since = utcnow() - timedelta(days=7)
    with Session(engine) as session:
        logs = session.query(AuditLog).filter(AuditLog.created_at >= since).all()
    counts = {}
    for l in logs:
        counts[l.event_type] = counts.get(l.event_type, 0) + 1
    return {
        "reminders_sent": counts.get("reminder", 0) + counts.get("retry", 0),
        "confirmed": counts.get("confirm", 0),
        "rescheduled": counts.get("reschedule", 0),
        "completed": counts.get("complete", 0),
        "missed": counts.get("missed", 0),
        "escalations": counts.get("escalation", 0),
    }


@app.post("/reminders/run")
def reminders_run():
    return {"reminders_sent": run_daily_reminders()}


@app.post("/reminders/retry")
def reminders_retry(force: bool = False):
    return run_retry_escalation(force=force)


@app.get("/audit")
def audit(limit: int = 50):
    with Session(engine) as session:
        logs = session.query(AuditLog).order_by(
            AuditLog.created_at.desc()).limit(limit).all()
        return [
            {"id": l.id, "event_type": l.event_type,
             "patient_phone": l.patient_phone, "description": l.description,
             "decided_by": l.decided_by, "outcome": l.outcome,
             "created_at": l.created_at.isoformat() if l.created_at else None}
            for l in logs
        ]


@app.get("/messages")
def messages(limit: int = 50):
    with Session(engine) as session:
        logs = session.query(MessageLog).order_by(
            MessageLog.created_at.desc()).limit(limit).all()
        return [
            {"id": l.id, "patient_phone": l.patient_phone,
             "direction": l.direction, "channel": l.channel,
             "message": l.message, "action_taken": l.action_taken,
             "created_at": l.created_at.isoformat() if l.created_at else None}
            for l in logs
        ]


@app.get("/health")
def health():
    return {"status": "ok", "clinic": CLINIC_NAME}


@app.get("/whatsapp/status")
def whatsapp_status():
    """Check whether real Twilio WhatsApp sending is configured."""
    configured = twilio_configured()
    return {
        "send_real": os.getenv("SEND_REAL_WHATSAPP", "true").strip().lower() == "true",
        "twilio_configured": configured,
        "from_number": os.getenv("TWILIO_WHATSAPP_NUMBER"),
        "mode": "template" if reminders.TWILIO_CONTENT_SID else "freeform",
        "content_sid": reminders.TWILIO_CONTENT_SID or None,
        "last_error": reminders.LAST_ERROR,
        "note": "Add real TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN to .env and join the "
                "Twilio sandbox from the recipient phone to deliver real WhatsApp messages."
                if not configured else "Ready to send via Twilio.",
    }


@app.get("/whatsapp/test")
def whatsapp_test(to: Optional[str] = None, sid: Optional[str] = None,
                  vars: Optional[str] = None):
    """Send a one-off test WhatsApp to `to` (defaults to DEMO_PHONE) and report the result.

    `sid` overrides TWILIO_CONTENT_SID; `vars` is a JSON string of template variables.
    """
    target = to or os.getenv("DEMO_PHONE")
    normalized = reminders.normalize_phone(target) if target else None
    content_sid = sid or reminders.TWILIO_CONTENT_SID
    if content_sid:
        if vars:
            try:
                variables = json.loads(vars)
            except Exception as e:  # noqa: BLE001
                return {"error": f"bad vars JSON: {e}"}
        else:
            variables = reminders.build_content_variables(None, None)
        ok = reminders.send_template(target, variables, content_sid)
        return {"to": normalized, "mode": "template", "from": os.getenv("TWILIO_WHATSAPP_NUMBER"),
                "content_sid": content_sid, "variables": variables,
                "delivered": ok, "error": reminders.LAST_ERROR}
    ok = send_whatsapp(target, f"Test message from {CLINIC_NAME} dashboard.")
    return {"to": normalized, "mode": "freeform",
            "from": os.getenv("TWILIO_WHATSAPP_NUMBER"), "delivered": ok,
            "error": reminders.LAST_ERROR}


# ------------------------------------------------------------------ scheduler

scheduler = BackgroundScheduler(timezone=ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata")))


@app.on_event("startup")
def _startup():
    Base.metadata.create_all(engine)
    if not scheduler.running:
        scheduler.add_job(run_daily_reminders, "cron", hour=8, minute=0,
                          id="daily_reminders", replace_existing=True)
        scheduler.add_job(run_retry_escalation, "interval", minutes=30,
                          id="retry_escalation", replace_existing=True)
        scheduler.start()


@app.on_event("shutdown")
def _shutdown():
    if scheduler.running:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8009")))
