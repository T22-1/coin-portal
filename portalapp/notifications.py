import logging

from django.conf import settings
from django.core.mail import send_mail


logger = logging.getLogger(__name__)


def send_urgent_task_email(task) -> bool:
    recipient = (task.assigned_to.email or "").strip()
    if not recipient:
        return False

    if settings.EMAIL_BACKEND.endswith("console.EmailBackend"):
        logger.warning(
            "Urgent task %s was not emailed because no mail provider is configured.",
            task.pk,
        )
        return False

    due_date = task.due_date.strftime("%B %d, %Y").replace(" 0", " ") if task.due_date else "No due date"
    creator = task.created_by.get_full_name() or task.created_by.get_username()
    body = "\n".join(
        [
            f"Hello {task.assigned_to.get_full_name() or task.assigned_to.get_username()},",
            "",
            "You have been assigned an urgent task in CoinPortal 365.",
            "",
            f"Task: {task.title}",
            f"Due: {due_date}",
            f"Assigned by: {creator}",
            "",
            task.details or "No additional details were provided.",
            "",
            f"Open your tasks: {settings.PORTAL_BASE_URL}/tasks/",
        ]
    )
    try:
        sent = send_mail(
            subject=f"[URGENT] Task assigned: {task.title}",
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient],
            fail_silently=False,
        )
    except Exception:
        logger.exception("Could not email urgent task %s to %s", task.pk, recipient)
        return False
    return sent == 1
