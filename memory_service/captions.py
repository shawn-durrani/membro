"""Machine-generated image descriptions, so photos stop being invisible (#107).

An image attachment stores its bytes but ``extract_text`` yields "" — so search,
mining, and recall never see it, and a conversation held *about* photos leaves
no trace of what the photos showed. This module writes a literal description
into the append-only ``attachment_captions`` table; the mining view and the
attachment FTS overlay it wherever ``extracted_text`` is empty.

Design constraints, each deliberate:

* **Runs at distill time, never at ingest.** Ingest is a synchronous HTTP call
  on the chat app's save path; a model call per image there would stall it.
  Distill is already an async job — captions ride it, right before mining reads
  the messages, and a caption is written once and reused forever after.
* **Grounded, or generic.** A captioner can confabulate exactly like a miner
  (a captioner confidently naming timber species from a photo is the motivating failure). The
  prompt demands only what is visible, transcription of legible text, and
  generic terms under uncertainty — never species/brand/person identification
  that isn't literally written in the image.
* **Marked as machine-made.** Every caption carries a visible prefix naming the
  model, so a ledger fact mined from a caption is traceable to a machine
  description rather than to something the user said.
* **Ground truth untouched — literally.** The attachments row is part of the
  episodic record and no module may UPDATE it (tested, and this module extends
  that test to its own table). Captions are appended alongside and overlaid at
  read time; the original row and bytes are never written to again.
* **Failure never blocks mining.** Missing key, oversized file, unsupported
  format, API error: the image is skipped (logged, retried on the next distill)
  and text mining proceeds as today.
"""

import base64
import logging

from . import db, episodic, llm

log = logging.getLogger("memory_service.captions")

# Formats the vision APIs accept. Anything else (HEIC, TIFF, SVG...) is skipped
# rather than guessed at; the bytes stay on disk untouched.
SUPPORTED_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

# API request ceiling is ~5 MB per image; stay under it rather than resizing
# (no image libraries in this service's dependency set, on purpose).
MAX_IMAGE_BYTES = 4_500_000

CAPTION_MARKER = "Image description (auto-generated"

_PROMPT = (
    "Describe this image for a personal memory record.\n"
    "Rules — follow them exactly:\n"
    "- State only what is literally visible.\n"
    "- Transcribe any readable text exactly as written.\n"
    "- Count objects when they are countable.\n"
    "- Include visible dimensions/markings only if legible.\n"
    "- Do NOT identify wood/plant/animal species, brands, product models, "
    "places, or people unless the name is visibly written in the image. "
    "When uncertain, use generic terms ('softwood boards', 'a handheld power "
    "tool').\n"
    "- No guesses about purpose, story, or what happened off-frame.\n"
    "At most 120 words."
)


def pending_images(con, conversation_id: int) -> list[dict]:
    """Image attachments in this conversation still lacking any extracted text."""
    return [dict(r) for r in con.execute(
        "SELECT id, filename, mime, size, stored_name FROM attachments "
        "WHERE conversation_id=? AND extracted_text='' AND mime LIKE 'image/%' "
        "AND id NOT IN (SELECT attachment_id FROM attachment_captions) "
        "ORDER BY id", (conversation_id,))]


def caption_pending(con, settings, conversation_id: int, *, vision=None) -> dict:
    """Backfill captions for this conversation's uncaptioned images.

    Returns {"captioned": n, "skipped": n}. ``vision`` is the test injection
    point; default is :func:`llm.utility_vision` on the miner model.
    """
    vision = vision or llm.utility_vision
    # Which model captions: caption_model when set, else the miner's. Resolved
    # once here so the marker and the provenance column name the model that
    # actually ran, not an assumption.
    model = settings.caption_model or settings.miner_model
    captioned = skipped = 0
    for att in pending_images(con, conversation_id):
        if att["mime"] not in SUPPORTED_MIMES or att["size"] > MAX_IMAGE_BYTES:
            skipped += 1
            continue
        path = episodic._attach_dir(settings) / att["stored_name"]
        try:
            data = path.read_bytes()
        except OSError as exc:
            log.warning("caption: cannot read %s (%s) — skipped", att["filename"], exc)
            skipped += 1
            continue
        try:
            text = vision(_PROMPT, [(att["mime"], base64.b64encode(data).decode())],
                          settings, model=model)
        except llm.MissingKeyError:
            # No vision-capable key: leave every image pending and say so once.
            log.warning("caption: no utility-model key — %d image(s) left "
                        "uncaptioned this run", len(pending_images(con, conversation_id)))
            skipped += 1
            break
        except Exception as exc:  # per-image: one bad file must not stop the rest
            log.warning("caption failed for %s (%s) — will retry next distill",
                        att["filename"], exc)
            skipped += 1
            continue
        text = (text or "").strip()
        if not text:
            skipped += 1
            continue
        # Append-only, own table: the attachments row is never touched (its
        # no-UPDATE invariant is tested). One caption per attachment, forever.
        con.execute(
            "INSERT OR IGNORE INTO attachment_captions"
            "(attachment_id, caption, model, created_at) VALUES(?,?,?,?)",
            (att["id"], f"[{CAPTION_MARKER} by {model}): {text}]",
             model, db.now()))
        con.commit()
        captioned += 1
    return {"captioned": captioned, "skipped": skipped}
