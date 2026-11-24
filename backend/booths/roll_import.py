"""Import booth-wise electoral rolls from CSV.

Expected columns (header row required, extra columns ignored):

    part_no, booth_name, serial_no, epic, name,
    relative_name, house_no, age, gender,
    booth_address (optional), cluster (optional)

Re-importing is safe: voters are matched on (booth, serial_no) and only the
roll fields are updated. Campaign data (status, notes, consent) is kept.
Electoral rolls are usually published as PDFs; convert them to this CSV
format first (OCR + manual check for scanned rolls).
"""

import csv
import io

from django.db import transaction

from .models import Booth, Cluster, Voter

REQUIRED = {"part_no", "serial_no", "epic", "name"}
GENDER = {"m": "M", "male": "M", "पुरुष": "M", "f": "F", "female": "F", "महिला": "F",
          "t": "T", "third gender": "T", "other": "T", "अन्य": "T"}


class RollImportError(Exception):
    pass


def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@transaction.atomic
def import_roll(fileobj, constituency) -> dict:
    raw = fileobj.read()
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
    reader = csv.DictReader(io.StringIO(text))
    headers = {h.strip().lower() for h in (reader.fieldnames or [])}
    missing = REQUIRED - headers
    if missing:
        raise RollImportError(f"Missing column(s): {', '.join(sorted(missing))}")

    booths, clusters = {}, {}
    created = updated = skipped = 0
    errors = []
    for line_no, row in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        part_no, serial_no = _int(row.get("part_no")), _int(row.get("serial_no"))
        if not part_no or not serial_no or not row.get("epic") or not row.get("name"):
            skipped += 1
            if len(errors) < 20:
                errors.append(f"Line {line_no}: part_no, serial_no, epic and name are required")
            continue

        booth = booths.get(part_no)
        if booth is None:
            booth, _ = Booth.objects.get_or_create(
                constituency=constituency, number=part_no,
                defaults={"name": row.get("booth_name") or f"Booth {part_no}",
                          "address": row.get("booth_address", "")},
            )
            cname = row.get("cluster")
            if cname and booth.cluster_id is None:
                cluster = clusters.get(cname)
                if cluster is None:
                    cluster, _ = Cluster.objects.get_or_create(constituency=constituency, name=cname)
                    clusters[cname] = cluster
                booth.cluster = cluster
                booth.save(update_fields=["cluster"])
            booths[part_no] = booth

        roll_fields = {
            "epic": row["epic"].upper(),
            "name": row["name"],
            "relative_name": row.get("relative_name", ""),
            "house_no": row.get("house_no", ""),
            "age": _int(row.get("age")),
            "gender": GENDER.get(row.get("gender", "").lower(), ""),
        }
        _, was_created = Voter.objects.update_or_create(
            booth=booth, serial_no=serial_no, defaults=roll_fields
        )
        created += was_created
        updated += not was_created

    return {"booths": len(booths), "created": created, "updated": updated,
            "skipped": skipped, "errors": errors}
