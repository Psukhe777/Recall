
Copy

# ============================================================
# RECALL SaaS — FastAPI Backend (PRODUCTION VERSION)
# Healthcare-agnostic automated SMS recall engine
# Stack: FastAPI + Supabase + Twilio
# ALL CRITICAL FIXES IMPLEMENTED ✅
# ============================================================

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, validator
from typing import Optional, List, Dict, Any
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from enum import Enum
import httpx
import os
import re
import logging
import secrets
from supabase import create_client, Client
from twilio.rest import Client as TwilioClient
from twilio.request_validator import RequestValidator
from dotenv import load_dotenv

# Rate limiting
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("recall")

# ============================================================
# CONFIG & VALIDATION
# ============================================================
required_env_vars = [
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_FROM_NUMBER",
    "API_SECRET_KEY"
]

missing = [var for var in required_env_vars if not os.getenv(var)]
if missing:
    raise RuntimeError(f"❌ Missing required environment variables: {', '.join(missing)}")

SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_SERVICE_KEY"]
TWILIO_SID      = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_TOKEN    = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM     = os.environ["TWILIO_FROM_NUMBER"]
API_SECRET      = os.environ["API_SECRET_KEY"]
ENVIRONMENT     = os.getenv("ENVIRONMENT", "development")

supabase: Client  = create_client(SUPABASE_URL, SUPABASE_KEY)
twilio            = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
twilio_validator  = RequestValidator(TWILIO_TOKEN)

# ============================================================
# APP WITH RATE LIMITING
# ============================================================
app = FastAPI(
    title="Recall SaaS API",
    description="Healthcare-agnostic automated patient recall via SMS",
    version="2.0.0",
)

# Rate limiter setup
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if ENVIRONMENT == "development" else os.getenv("ALLOWED_ORIGINS", "").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# MODELS
# ============================================================
class RecallStatus(str, Enum):
    pending    = "pending"
    in_progress = "in_progress"
    booked     = "booked"
    completed  = "completed"
    opted_out  = "opted_out"
    failed     = "failed"
    snoozed    = "snoozed"

class PatientCreate(BaseModel):
    external_id:    Optional[str]   = None
    first_name:     str
    last_name:      str
    phone:          str
    email:          Optional[str]   = None
    date_of_birth:  Optional[date]  = None
    metadata:       Dict[str, Any]  = {}

    @validator("phone")
    def validate_phone(cls, v):
        if not re.match(r"^\+\d{7,15}$", v):
            raise ValueError("Phone must be E.164 format: +61412345678")
        return v

class TenantConfig(BaseModel):
    name:           str
    slug:           str
    service_type:   str
    phone_number:   Optional[str]   = None
    timezone:       str             = "UTC"
    country_code:   str             = "AU"
    twilio_from:    Optional[str]   = None
    settings:       Dict[str, Any]  = {}

class RecallCreate(BaseModel):
    patient_id:      str
    template_id:     Optional[str]  = None
    recall_type:     str
    last_appointment: Optional[date] = None
    due_date:        date
    booking_link:    Optional[str]  = None
    notes:           Optional[str]  = None
    priority:        int            = 1

    @validator("due_date")
    def validate_due_date(cls, v):
        if v < date.today():
            raise ValueError("Due date cannot be in the past")
        return v

class BulkImportRequest(BaseModel):
    patients:        List[Dict[str, Any]]
    template_id:     Optional[str] = None
    recall_type:     str
    recall_interval_days: int = 180

# ============================================================
# AUTH & SECURITY
# ============================================================
def verify_api_key(x_api_key: str = Header(...)):
    """Verify global admin API key"""
    if x_api_key != API_SECRET:
        raise HTTPException(401, "Invalid API key")
    return True

def verify_tenant_auth(tenant_id: str, authorization: str = Header(...)):
    """
    Verify Bearer token matches tenant's API key.
    This protects per-tenant endpoints.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Invalid authorization header format. Use 'Bearer <token>'")
    
    try:
        token = authorization.replace("Bearer ", "").strip()
        
        # Get tenant and verify key
        tenant_res = supabase.table("tenants").select("*").eq("id", tenant_id).single().execute()
        tenant = tenant_res.data
        
        if not tenant:
            raise HTTPException(404, "Tenant not found")
        
        if tenant.get("api_key") != token:
            raise HTTPException(401, "Invalid tenant API key")
        
        if not tenant.get("active", True):
            raise HTTPException(403, "Tenant account is inactive")
        
        return tenant
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Tenant auth error: {e}")
        raise HTTPException(401, "Authentication failed")

def get_tenant(tenant_id: str) -> dict:
    """Get tenant by ID (for internal use)"""
    res = supabase.table("tenants").select("*").eq("id", tenant_id).single().execute()
    if not res.data:
        raise HTTPException(404, "Tenant not found")
    return res.data

def get_tenant_timezone(tenant: dict) -> ZoneInfo:
    """Get tenant's timezone as ZoneInfo object"""
    tz_str = tenant.get("timezone", "UTC")
    try:
        return ZoneInfo(tz_str)
    except Exception as e:
        logger.warning(f"Invalid timezone {tz_str}, falling back to UTC: {e}")
        return ZoneInfo("UTC")

# ============================================================
# SMS ENGINE
# ============================================================
def render_template(template: str, patient: dict, tenant: dict, recall: dict) -> str:
    """Replace template variables with actual values"""
    replacements = {
        "{first_name}":      patient.get("first_name", ""),
        "{last_name}":       patient.get("last_name", ""),
        "{full_name}":       f"{patient.get('first_name','')} {patient.get('last_name','')}",
        "{practice_name}":   tenant.get("name", "Your practice"),
        "{practice_phone}":  tenant.get("phone_number", ""),
        "{booking_link}":    recall.get("booking_link", ""),
        "{pet_name}":        patient.get("metadata", {}).get("pet_name", "your pet"),
        "{due_date}":        recall.get("due_date", ""),
    }
    msg = template
    for key, val in replacements.items():
        msg = msg.replace(key, str(val))
    return msg

def send_sms(to: str, body: str, tenant: dict, recall_id: str = None,
             patient_id: str = None, sequence_step: int = 0) -> dict:
    """Send SMS via Twilio and log to DB"""
    from_number = tenant.get("twilio_from") or TWILIO_FROM

    try:
        msg = twilio.messages.create(to=to, from_=from_number, body=body)
        status = "sent"
        twilio_sid = msg.sid
        error_code = None
        error_msg = None
    except Exception as e:
        logger.error(f"SMS send failed to {to}: {e}")
        status = "failed"
        twilio_sid = None
        error_code = "SEND_ERROR"
        error_msg = str(e)

    # Log to DB
    log_entry = {
        "tenant_id":       tenant["id"],
        "patient_id":      patient_id,
        "recall_id":       recall_id,
        "twilio_sid":      twilio_sid,
        "direction":       "outbound",
        "from_number":     from_number,
        "to_number":       to,
        "body":            body,
        "status":          status,
        "error_code":      error_code,
        "error_message":   error_msg,
        "sequence_step":   sequence_step,
        "sent_at":         datetime.utcnow().isoformat() if status == "sent" else None,
    }
    supabase.table("sms_messages").insert(log_entry).execute()
    return {"status": status, "twilio_sid": twilio_sid}

# ============================================================
# INBOUND SMS HANDLER (FIXED)
# ============================================================
def detect_intent(body: str) -> str:
    """Detect user intent from SMS body"""
    body_lower = body.lower().strip()
    
    # STOP intent (including opt-out variations)
    if any(kw in body_lower for kw in ["stop", "cancel", "unsubscribe", "remove", "opt out", "optout"]):
        return "STOP"
    
    # START intent (opt back in)
    if any(kw in body_lower for kw in ["start", "unstop", "subscribe", "opt in", "optin"]):
        return "START"
    
    # BOOK intent
    if any(kw in body_lower for kw in ["book", "yes", "y", "1", "confirm", "schedule", "ok", "sure", "please"]):
        return "BOOK"
    
    # SNOOZE intent
    if any(kw in body_lower for kw in ["later", "not now", "snooze", "remind me", "next month", "maybe"]):
        return "SNOOZE"
    
    return "UNKNOWN"

def handle_inbound_sms(from_num: str, to_num: str, body: str, sid: str) -> str:
    """
    Handle inbound SMS from patient (FIXED VERSION)
    - Looks up patient and recall
    - Updates database based on intent
    - Returns appropriate response message
    """
    try:
        # Find patient by phone number
        patient_res = supabase.table("patients").select("*").eq("phone", from_num).execute()
        
        if not patient_res.data:
            logger.warning(f"Inbound SMS from unknown number: {from_num}")
            return "Thanks for your message. We couldn't find your number in our system. Please contact your healthcare provider directly."
        
        patient = patient_res.data[0]
        
        # Get tenant info
        tenant = get_tenant(patient["tenant_id"])
        
        # Detect intent
        intent = detect_intent(body)
        logger.info(f"Inbound SMS from {from_num}: intent={intent}, body='{body[:50]}'")
        
        # Find active recall for this patient
        recall_res = supabase.table("recalls").select("*").eq("patient_id", patient["id"]).in_(
            "status", ["pending", "in_progress", "snoozed"]
        ).order("created_at", desc=True).limit(1).execute()
        
        recall = recall_res.data[0] if recall_res.data else None
        
        # Log the inbound message
        supabase.table("inbound_responses").insert({
            "tenant_id": patient["tenant_id"],
            "patient_id": patient["id"],
            "recall_id": recall["id"] if recall else None,
            "from_number": from_num,
            "to_number": to_num,
            "body": body,
            "intent": intent,
            "twilio_sid": sid,
            "handled": True,
            "received_at": datetime.utcnow().isoformat()
        }).execute()
        
        # Handle STOP intent
        if intent == "STOP":
            supabase.table("patients").update({
                "opted_out": True,
                "opted_out_at": datetime.utcnow().isoformat()
            }).eq("id", patient["id"]).execute()
            
            # Mark all active recalls as opted_out
            if recall:
                supabase.table("recalls").update({
                    "status": "opted_out"
                }).eq("patient_id", patient["id"]).in_("status", ["pending", "in_progress", "snoozed"]).execute()
            
            logger.info(f"Patient {patient['id']} opted out")
            return "You've been opted out from all recall messages. Reply START anytime to opt back in."
        
        # Handle START intent (opt back in)
        elif intent == "START":
            supabase.table("patients").update({
                "opted_out": False,
                "opted_in_at": datetime.utcnow().isoformat()
            }).eq("id", patient["id"]).execute()
            
            logger.info(f"Patient {patient['id']} opted back in")
            return f"Welcome back! You're now subscribed to recall messages from {tenant['name']}."
        
        # Handle BOOK intent
        elif intent == "BOOK" and recall:
            # Mark recall as booked
            supabase.table("recalls").update({
                "status": "booked",
                "booked_at": datetime.utcnow().isoformat()
            }).eq("id", recall["id"]).execute()
            
            # Create booking record
            supabase.table("bookings").insert({
                "tenant_id": patient["tenant_id"],
                "patient_id": patient["id"],
                "recall_id": recall["id"],
                "source": "sms_reply",
                "status": "pending_confirmation"
            }).execute()
            
            logger.info(f"Recall {recall['id']} marked as booked via SMS")
            return f"Perfect! We've noted you'd like to book an appointment. Someone from {tenant['name']} will call you soon to confirm a time."
        
        # Handle SNOOZE intent
        elif intent == "SNOOZE" and recall:
            # Snooze for 14 days
            snooze_until = datetime.utcnow() + timedelta(days=14)
            supabase.table("recalls").update({
                "status": "snoozed",
                "snoozed_until": snooze_until.date().isoformat(),
                "next_send_at": snooze_until.isoformat()
            }).eq("id", recall["id"]).execute()
            
            logger.info(f"Recall {recall['id']} snoozed until {snooze_until.date()}")
            return "No problem! We'll remind you again in 2 weeks. 👍"
        
        else:
            # Unknown intent or no active recall
            if not recall:
                return f"Thanks for your message! We don't have any active recalls for you right now. Call {tenant.get('phone_number', 'us')} if you need to book."
            else:
                return f"Thanks for your message! Reply BOOK to schedule, LATER to remind you in 2 weeks, or STOP to unsubscribe."
    
    except Exception as e:
        logger.error(f"Error handling inbound SMS: {e}")
        return "We received your message but encountered an error. Please call us directly."

# ============================================================
# RECALL PROCESSOR (FIXED WITH ERROR HANDLING & TIMEZONE)
# ============================================================
def _process_single_recall(recall: dict):
    """Process a single recall - send next message in sequence"""
    patient = recall["patients"]
    tenant = recall["tenants"]
    template = recall.get("recall_templates")
    
    # Skip if patient opted out
    if patient.get("opted_out"):
        logger.info(f"Skipping recall {recall['id']} - patient opted out")
        supabase.table("recalls").update({"status": "opted_out"}).eq("id", recall["id"]).execute()
        return
    
    # Get message sequence
    if template and template.get("message_sequence"):
        sequence = template["message_sequence"]
    else:
        logger.warning(f"No template sequence for recall {recall['id']}")
        return
    
    step = recall.get("sequence_step", 0)
    
    if step >= len(sequence):
        # End of sequence - mark as completed
        supabase.table("recalls").update({"status": "completed"}).eq("id", recall["id"]).execute()
        logger.info(f"Recall {recall['id']} completed (end of sequence)")
        return
    
    # Get current message
    msg_config = sequence[step]
    message_text = render_template(msg_config["message"], patient, tenant, recall)
    
    # Send SMS
    result = send_sms(
        to=patient["phone"],
        body=message_text,
        tenant=tenant,
        recall_id=recall["id"],
        patient_id=patient["id"],
        sequence_step=step
    )
    
    # Calculate next send time (using tenant timezone)
    next_step = step + 1
    if next_step < len(sequence):
        next_delay_days = sequence[next_step].get("delay_days", 7)
        
        # Get tenant timezone
        tenant_tz = get_tenant_timezone(tenant)
        
        # Calculate next send time in tenant's local time (9am)
        now_local = datetime.now(tenant_tz)
        next_send_local = now_local + timedelta(days=next_delay_days)
        next_send_local = next_send_local.replace(hour=9, minute=0, second=0, microsecond=0)
        
        # Convert back to UTC for storage
        next_send_at = next_send_local.astimezone(ZoneInfo("UTC"))
        new_status = "in_progress"
    else:
        next_send_at = None
        new_status = "in_progress"  # Will be marked completed on next run
    
    # Update recall
    updates = {
        "sequence_step": next_step,
        "next_send_at": next_send_at.isoformat() if next_send_at else None,
        "status": new_status if result["status"] == "sent" else "failed",
        "last_sent_at": datetime.utcnow().isoformat()
    }
    
    supabase.table("recalls").update(updates).eq("id", recall["id"]).execute()
    logger.info(f"Processed recall {recall['id']}, sent step {step}, status: {result['status']}")

def process_due_recalls():
    """
    Find all recalls due to send next message and dispatch SMS
    FIXED WITH ERROR HANDLING & PROPER TIMEZONE SUPPORT
    """
    now = datetime.utcnow().isoformat()
    
    try:
        # Fetch recalls due for next message
        res = supabase.table("recalls").select(
            "*, patients(*), tenants(*), recall_templates(*)"
        ).in_("status", ["pending", "in_progress", "snoozed"]).lte("next_send_at", now).execute()
        
        recalls = res.data or []
        logger.info(f"Found {len(recalls)} recalls to process")
        
        success_count = 0
        error_count = 0
        
        for recall in recalls:
            try:
                _process_single_recall(recall)
                success_count += 1
            except Exception as e:
                error_count += 1
                logger.error(f"Failed to process recall {recall['id']}: {e}")
                
                # Mark recall as failed with error details
                try:
                    supabase.table("recalls").update({
                        "status": "failed",
                        "notes": f"Processing error: {str(e)[:200]}"
                    }).eq("id", recall["id"]).execute()
                except Exception as update_error:
                    logger.error(f"Failed to update failed status for recall {recall['id']}: {update_error}")
        
        logger.info(f"Recall processing complete: {success_count} success, {error_count} errors")
        return {"success": success_count, "errors": error_count, "total": len(recalls)}
    
    except Exception as e:
        logger.error(f"Fatal error in process_due_recalls: {e}")
        raise

# ============================================================
# ENDPOINTS
# ============================================================

@app.get("/")
def root():
    return {"service": "Recall SaaS API", "version": "2.0.0", "status": "operational"}

@app.get("/health")
def health_check():
    """Enhanced health check with DB and Twilio connectivity tests"""
    health_status = {
        "status": "healthy",
        "database": "unknown",
        "twilio": "unknown",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "2.0.0"
    }
    
    # Test database connection
    try:
        supabase.table("tenants").select("id").limit(1).execute()
        health_status["database"] = "connected"
    except Exception as e:
        health_status["database"] = "error"
        health_status["status"] = "degraded"
        logger.error(f"Database health check failed: {e}")
    
    # Test Twilio
    try:
        twilio.api.accounts(TWILIO_SID).fetch()
        health_status["twilio"] = "connected"
    except Exception as e:
        health_status["twilio"] = "error"
        health_status["status"] = "degraded"
        logger.error(f"Twilio health check failed: {e}")
    
    return health_status

# -- Tenants (FIXED WITH API KEY GENERATION) --
@app.post("/tenants")
@limiter.limit("10/minute")
def create_tenant(request: Request, config: TenantConfig, x_api_key: str = Header(...)):
    """Create a new tenant with auto-generated API key"""
    verify_api_key(x_api_key)
    
    # Generate unique API key for tenant
    tenant_api_key = f"sk_{config.slug}_{secrets.token_urlsafe(32)}"
    
    data = {
        "name": config.name,
        "slug": config.slug,
        "service_type": config.service_type,
        "phone_number": config.phone_number,
        "timezone": config.timezone,
        "country_code": config.country_code,
        "twilio_from": config.twilio_from,
        "settings": config.settings,
        "api_key": tenant_api_key,
        "active": True
    }
    
    try:
        res = supabase.table("tenants").insert(data).execute()
        logger.info(f"Created tenant: {res.data[0]['id']} with slug: {config.slug}")
        return res.data[0]
    except Exception as e:
        logger.error(f"Failed to create tenant: {e}")
        raise HTTPException(500, f"Failed to create tenant: {str(e)}")

@app.get("/tenants/{tenant_id}")
def get_tenant_info(tenant_id: str, tenant: dict = Depends(verify_tenant_auth)):
    """Get tenant information"""
    return tenant

# -- Templates --
@app.get("/templates")
def list_templates(service_type: Optional[str] = None):
    """List global recall templates"""
    query = supabase.table("recall_templates").select("*").is_("tenant_id", "null")
    if service_type:
        query = query.eq("service_type", service_type)
    res = query.order("service_type").execute()
    return {"data": res.data or []}

@app.get("/tenants/{tenant_id}/templates")
def list_tenant_templates(tenant_id: str, tenant: dict = Depends(verify_tenant_auth)):
    """List tenant-specific templates + global templates"""
    # Get tenant templates
    tenant_res = supabase.table("recall_templates").select("*").eq("tenant_id", tenant_id).execute()
    
    # Get global templates for this service type
    global_res = supabase.table("recall_templates").select("*").is_("tenant_id", "null").eq(
        "service_type", tenant["service_type"]
    ).execute()
    
    return {"data": (tenant_res.data or []) + (global_res.data or [])}

# -- Patients (FIXED WITH AUTH) --
@app.post("/tenants/{tenant_id}/patients")
@limiter.limit("100/minute")
def create_patient(request: Request, tenant_id: str, patient: PatientCreate, tenant: dict = Depends(verify_tenant_auth)):
    """Create a new patient"""
    data = {
        "tenant_id": tenant_id,
        "external_id": patient.external_id,
        "first_name": patient.first_name,
        "last_name": patient.last_name,
        "phone": patient.phone,
        "email": patient.email,
        "date_of_birth": patient.date_of_birth.isoformat() if patient.date_of_birth else None,
        "metadata": patient.metadata,
    }
    
    try:
        res = supabase.table("patients").insert(data).execute()
        logger.info(f"Created patient: {res.data[0]['id']}")
        return res.data[0]
    except Exception as e:
        logger.error(f"Failed to create patient: {e}")
        raise HTTPException(500, f"Failed to create patient: {str(e)}")

@app.get("/tenants/{tenant_id}/patients")
def list_patients(
    tenant_id: str,
    tenant: dict = Depends(verify_tenant_auth),
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
):
    """List patients with optional search"""
    query = supabase.table("patients").select("*").eq("tenant_id", tenant_id)
    
    if search:
        # Search in name or phone
        query = query.or_(f"first_name.ilike.%{search}%,last_name.ilike.%{search}%,phone.ilike.%{search}%")
    
    res = query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
    return {"data": res.data or [], "total": len(res.data or [])}

# -- Recalls (FIXED WITH AUTH) --
@app.post("/tenants/{tenant_id}/recalls")
@limiter.limit("100/minute")
def create_recall(request: Request, tenant_id: str, recall: RecallCreate, tenant: dict = Depends(verify_tenant_auth)):
    """Create a new recall"""
    
    # Validate template if provided
    if recall.template_id:
        t_res = supabase.table("recall_templates").select("*").eq("id", recall.template_id).single().execute()
        if not t_res.data:
            raise HTTPException(404, "Template not found")
    
    # Calculate first send time (using tenant timezone)
    tenant_tz = get_tenant_timezone(tenant)
    due_dt_local = datetime.combine(recall.due_date, datetime.min.time()).replace(tzinfo=tenant_tz)
    
    # Set to 9am local time on due date
    due_dt_local = due_dt_local.replace(hour=9, minute=0, second=0, microsecond=0)
    
    # Convert to UTC for storage
    first_send_utc = due_dt_local.astimezone(ZoneInfo("UTC"))
    
    # Don't send in the past
    now_utc = datetime.now(ZoneInfo("UTC"))
    if first_send_utc < now_utc:
        first_send_utc = now_utc
    
    data = {
        "tenant_id": tenant_id,
        "patient_id": recall.patient_id,
        "template_id": recall.template_id,
        "recall_type": recall.recall_type,
        "last_appointment": recall.last_appointment.isoformat() if recall.last_appointment else None,
        "due_date": recall.due_date.isoformat(),
        "status": "pending",
        "sequence_step": 0,
        "next_send_at": first_send_utc.isoformat(),
        "booking_link": recall.booking_link,
        "notes": recall.notes,
        "priority": recall.priority,
    }
    
    try:
        res = supabase.table("recalls").insert(data).execute()
        logger.info(f"Created recall: {res.data[0]['id']}")
        return res.data[0]
    except Exception as e:
        logger.error(f"Failed to create recall: {e}")
        raise HTTPException(500, f"Failed to create recall: {str(e)}")

@app.get("/tenants/{tenant_id}/recalls")
def list_recalls(
    tenant_id: str,
    tenant: dict = Depends(verify_tenant_auth),
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
):
    """List recalls with optional status filter"""
    query = supabase.table("recalls").select("*, patients(first_name, last_name, phone)").eq("tenant_id", tenant_id)
    
    if status:
        query = query.eq("status", status)
    
    res = query.order("due_date", desc=False).range(offset, offset + limit - 1).execute()
    return {"data": res.data or [], "total": len(res.data or [])}

@app.get("/tenants/{tenant_id}/recalls/{recall_id}")
def get_recall(tenant_id: str, recall_id: str, tenant: dict = Depends(verify_tenant_auth)):
    """Get a specific recall"""
    res = supabase.table("recalls").select(
        "*, patients(*), recall_templates(*)"
    ).eq("id", recall_id).eq("tenant_id", tenant_id).single().execute()
    
    if not res.data:
        raise HTTPException(404, "Recall not found")
    
    return res.data

@app.patch("/tenants/{tenant_id}/recalls/{recall_id}")
def update_recall(
    tenant_id: str,
    recall_id: str,
    updates: dict,
    tenant: dict = Depends(verify_tenant_auth)
):
    """Update a recall (status, notes, etc.)"""
    try:
        res = supabase.table("recalls").update(updates).eq("id", recall_id).eq("tenant_id", tenant_id).execute()
        if not res.data:
            raise HTTPException(404, "Recall not found")
        return res.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update recall: {e}")
        raise HTTPException(500, f"Failed to update recall: {str(e)}")

@app.post("/tenants/{tenant_id}/recalls/{recall_id}/send-now")
def send_recall_now(tenant_id: str, recall_id: str, tenant: dict = Depends(verify_tenant_auth)):
    """Manually trigger immediate send of next message in recall sequence"""
    # Fetch the recall
    recall_res = supabase.table("recalls").select(
        "*, patients(*), recall_templates(*)"
    ).eq("id", recall_id).eq("tenant_id", tenant_id).single().execute()
    
    if not recall_res.data:
        raise HTTPException(404, "Recall not found")
    
    recall = recall_res.data
    recall["tenants"] = tenant  # Add tenant info
    
    try:
        _process_single_recall(recall)
        return {"message": "Recall sent successfully", "recall_id": recall_id}
    except Exception as e:
        logger.error(f"Failed to send recall: {e}")
        raise HTTPException(500, f"Failed to send recall: {str(e)}")

@app.post("/tenants/{tenant_id}/recalls/bulk-import")
@limiter.limit("10/minute")
def bulk_import_recalls(
    request: Request,
    tenant_id: str,
    import_req: BulkImportRequest,
    background_tasks: BackgroundTasks,
    tenant: dict = Depends(verify_tenant_auth)
):
    """Bulk import patients and create recalls"""
    
    def _import_task():
        created_count = 0
        error_count = 0
        
        for patient_data in import_req.patients:
            try:
                # Create or update patient
                patient_insert = {
                    "tenant_id": tenant_id,
                    "first_name": patient_data["first_name"],
                    "last_name": patient_data["last_name"],
                    "phone": patient_data["phone"],
                    "email": patient_data.get("email"),
                    "external_id": patient_data.get("external_id"),
                }
                
                patient_res = supabase.table("patients").upsert(patient_insert).execute()
                patient = patient_res.data[0]
                
                # Create recall
                due_date_str = patient_data.get("due_date")
                if not due_date_str:
                    # Calculate from last_appointment + interval
                    last_apt = datetime.fromisoformat(patient_data.get("last_appointment"))
                    due_date = last_apt.date() + timedelta(days=import_req.recall_interval_days)
                else:
                    due_date = datetime.fromisoformat(due_date_str).date()
                
                # Calculate first send time (using tenant timezone)
                tenant_tz = get_tenant_timezone(tenant)
                first_send_local = datetime.combine(due_date, datetime.min.time()).replace(tzinfo=tenant_tz, hour=9)
                first_send_utc = first_send_local.astimezone(ZoneInfo("UTC"))
                
                recall_insert = {
                    "tenant_id": tenant_id,
                    "patient_id": patient["id"],
                    "template_id": import_req.template_id,
                    "recall_type": import_req.recall_type,
                    "due_date": due_date.isoformat(),
                    "status": "pending",
                    "next_send_at": first_send_utc.isoformat(),
                    "sequence_step": 0,
                }
                
                supabase.table("recalls").insert(recall_insert).execute()
                created_count += 1
                
            except Exception as e:
                error_count += 1
                logger.error(f"Bulk import error for patient {patient_data.get('phone')}: {e}")
        
        logger.info(f"Bulk import completed: {created_count} created, {error_count} errors")
    
    background_tasks.add_task(_import_task)
    return {"message": f"Bulk import of {len(import_req.patients)} patients queued", "count": len(import_req.patients)}

# -- Analytics --
@app.get("/tenants/{tenant_id}/analytics")
def get_analytics(
    tenant_id: str,
    tenant: dict = Depends(verify_tenant_auth),
    days: int = 30
):
    """Get recall and SMS analytics for tenant"""
    start_date = (datetime.utcnow() - timedelta(days=days)).isoformat()
    
    try:
        # Use the analytics function if it exists in Supabase
        res = supabase.rpc("get_tenant_analytics", {
            "tenant_id_param": tenant_id,
            "start_date_param": start_date
        }).execute()
        
        if res.data:
            return res.data
    except Exception as e:
        logger.warning(f"Analytics function not available, using fallback: {e}")
    
    # Fallback: manual aggregation
    recalls = supabase.table("recalls").select("*").eq("tenant_id", tenant_id).gte("created_at", start_date).execute()
    sms = supabase.table("sms_messages").select("*").eq("tenant_id", tenant_id).gte("created_at", start_date).execute()
    bookings = supabase.table("bookings").select("*").eq("tenant_id", tenant_id).gte("created_at", start_date).execute()
    
    recalls_data = recalls.data or []
    sms_data = sms.data or []
    bookings_data = bookings.data or []
    
    # Calculate stats
    total_recalls = len(recalls_data)
    status_counts = {}
    for r in recalls_data:
        status = r.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    
    booked_count = status_counts.get("booked", 0)
    conversion_rate = round((booked_count / total_recalls * 100), 1) if total_recalls > 0 else 0
    
    sms_sent = sum(1 for m in sms_data if m.get("status") == "sent")
    sms_failed = sum(1 for m in sms_data if m.get("status") == "failed")
    sms_delivered = sum(1 for m in sms_data if m.get("status") == "delivered")
    
    revenue_recovered = sum(b.get("revenue_amount", 0) for b in bookings_data)
    
    return {
        "recall_stats": {
            "total_recalls": total_recalls,
            **status_counts,
            "conversion_rate": conversion_rate
        },
        "sms_stats": {
            "sent": sms_sent,
            "delivered": sms_delivered,
            "failed": sms_failed
        },
        "revenue_recovered": revenue_recovered,
        "bookings_count": len(bookings_data),
        "period_days": days
    }

# -- Cron Jobs --
@app.post("/cron/process-recalls")
@limiter.limit("10/minute")
def cron_process_recalls(request: Request, x_api_key: str = Header(...)):
    """
    Cron endpoint: Process all due recalls
    Call this hourly via Supabase cron or external scheduler
    """
    verify_api_key(x_api_key)
    
    try:
        result = process_due_recalls()
        return {
            "message": "Recall processing completed",
            "timestamp": datetime.utcnow().isoformat(),
            **result
        }
    except Exception as e:
        logger.error(f"Cron job failed: {e}")
        raise HTTPException(500, f"Cron job failed: {str(e)}")

# -- Webhooks --
@app.post("/webhooks/twilio/inbound")
async def twilio_inbound_webhook(request: Request):
    """Handle inbound SMS from Twilio"""
    form = await request.form()
    
    # Validate Twilio signature (only in production)
    if ENVIRONMENT == "production":
        signature = request.headers.get("X-Twilio-Signature", "")
        url = str(request.url)
        
        if not twilio_validator.validate(url, dict(form), signature):
            logger.warning("Invalid Twilio signature")
            raise HTTPException(403, "Invalid Twilio signature")
    
    from_num = form.get("From")
    to_num = form.get("To")
    body = form.get("Body", "")
    sid = form.get("MessageSid")
    
    # Handle the message
    response_text = handle_inbound_sms(from_num, to_num, body, sid)
    
    # Return TwiML response
    return PlainTextResponse(
        content=f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{response_text}</Message></Response>',
        media_type="text/xml"
    )

@app.post("/webhooks/twilio/status")
async def twilio_status_webhook(request: Request):
    """Handle SMS status callbacks from Twilio"""
    form = await request.form()
    
    sid = form.get("MessageSid")
    status = form.get("MessageStatus")
    error_code = form.get("ErrorCode")
    
    # Update SMS log
    try:
        supabase.table("sms_messages").update({
            "status": status,
            "error_code": error_code,
            "delivered_at": datetime.utcnow().isoformat() if status == "delivered" else None
        }).eq("twilio_sid", sid).execute()
        
        logger.info(f"Updated SMS status: {sid} -> {status}")
    except Exception as e:
        logger.error(f"Failed to update SMS status: {e}")
    
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
