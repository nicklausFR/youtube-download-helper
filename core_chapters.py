"""Chapter interval composition and FFmetadata escaping."""


def _arranged_clip_chapters(native, sponsor, clip_start, clip_end):
    """Combine creator and SponsorBlock intervals, cropped to the extract."""
    clip_start = float(clip_start or 0)
    clip_end = None if clip_end is None else float(clip_end)
    native = [item for item in (native or []) if isinstance(item, dict)]
    sponsor = [item for item in (sponsor or []) if isinstance(item, dict)]
    boundaries = {clip_start}
    if clip_end is not None:
        boundaries.add(clip_end)
    for item in native + sponsor:
        try:
            start = max(clip_start, float(item.get("start_time", 0)))
            end = float(item.get("end_time"))
        except (TypeError, ValueError):
            continue
        if clip_end is not None:
            end = min(clip_end, end)
        if end > start:
            boundaries.update((start, end))
    points = sorted(boundaries)
    chapters = []
    for start, end in zip(points, points[1:]):
        midpoint = (start + end) / 2
        sponsors = [
            item for item in sponsor
            if float(item.get("start_time", float("inf"))) <= midpoint
            < float(item.get("end_time", float("-inf")))
        ]
        creators = [
            item for item in native
            if float(item.get("start_time", float("inf"))) <= midpoint
            < float(item.get("end_time", float("-inf")))
        ]
        if sponsors:
            names = list(dict.fromkeys(
                str(item.get("title") or item.get("category") or "Segment")
                for item in sponsors))
            title = "[SponsorBlock] : " + " / ".join(names)
        elif creators:
            title = str(creators[-1].get("title") or "Chapitre")
        else:
            continue
        if chapters and chapters[-1]["title"] == title and abs(
                chapters[-1]["end_time"] - start) < 0.002:
            chapters[-1]["end_time"] = end
        else:
            chapters.append({"start_time": start, "end_time": end, "title": title})
    return chapters


def _ffmetadata_escape(value):
    return str(value).replace("\\", "\\\\").replace("=", "\\=").replace(
        ";", "\\;").replace("#", "\\#").replace("\n", "\\n")
