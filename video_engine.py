"""Local, non-destructive ski footage review. Python 3.11+; no cloud service."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import cv2
import imageio_ffmpeg
import numpy as np
import review_features
import semantic
import media_timing

DEFAULT = dict(sample_fps=2, window_seconds=6, stride_seconds=3,
               min_clip_seconds=1.5, max_clip_seconds=24,
               pre_roll_seconds=.5, post_roll_seconds=.75, merge_gap_seconds=.5,
               candidates_per_file=20, min_score=15, profile="mixed",
               timeline_fps=30, width=1920, height=1080, max_export_seconds=None,
               categories=[], semantic_enabled=False)


def save_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_project(project):
    project = Path(project)
    state = read_json(project / "project.json")
    decisions_path = project / "decisions.json"
    decisions = read_json(decisions_path) if decisions_path.exists() else {}
    edits_path = project / 'edits.json'
    edits = read_json(edits_path) if edits_path.exists() else {}
    for candidate in state["candidates"]:
        candidate.update(edits.get(candidate['id'], {}))
        if candidate["id"] in decisions:
            candidate["decision"] = decisions[candidate["id"]]
    chronology = project / 'chronology.json'
    if chronology.exists():
        state['recording_times'] = read_json(chronology)
    return state


def record_decision(project, candidate_id, decision):
    if decision not in ("accepted", "rejected", "pending"):
        raise ValueError("Nieprawidłowa decyzja")
    state = load_project(project)
    if not any(x["id"] == candidate_id for x in state["candidates"]):
        raise ValueError("Nie znaleziono klipu")
    path = Path(project) / "decisions.json"
    decisions = read_json(path) if path.exists() else {}
    decisions[candidate_id] = decision
    # The analyzer owns project.json; review decisions have a separate writer.
    save_json(path, decisions)


def config(path=None):
    c = DEFAULT.copy()
    if path:
        extra = path if isinstance(path, dict) else read_json(path)
        if set(extra) - set(c):
            raise ValueError("Nieznane pola konfiguracji: " + str(set(extra) - set(c)))
        c.update(extra)
    for k in ("sample_fps", "window_seconds", "stride_seconds", "candidates_per_file",
              "timeline_fps", "width", "height",
              "min_clip_seconds", "max_clip_seconds"):
        if not isinstance(c[k], (int, float)) or not math.isfinite(c[k]) or c[k] <= 0:
            raise ValueError("Wymagana dodatnia liczba: " + k)
    for k in ("candidates_per_file", "timeline_fps", "width", "height"):
        if int(c[k]) != c[k]:
            raise ValueError("Wymagana liczba całkowita: " + k)
        c[k] = int(c[k])
    if c["timeline_fps"] not in (24, 25, 30, 50, 60):
        raise ValueError("timeline_fps: obsługiwane 24, 25, 30, 50, 60")
    if c["width"] % 2 or c["height"] % 2 or c["sample_fps"] > 10:
        raise ValueError("Parzyste wymiary; sample_fps <= 10")
    if c["profile"] not in ("mixed", "pov", "phone") or not 0 <= c["min_score"] <= 100:
        raise ValueError("Nieprawidłowy profil lub min_score")
    for key in ("pre_roll_seconds", "post_roll_seconds", "merge_gap_seconds"):
        if not isinstance(c[key], (int, float)) or not math.isfinite(c[key]) or c[key] < 0:
            raise ValueError("Wymagana nieujemna liczba: " + key)
    if c["min_clip_seconds"] > c["max_clip_seconds"]:
        raise ValueError("min_clip_seconds nie może przekraczać max_clip_seconds")
    if not isinstance(c['categories'], list) or any(x not in semantic.CATEGORIES for x in c['categories']):
        raise ValueError('Nieznane kategorie.')
    if not isinstance(c['semantic_enabled'], bool):
        raise ValueError('Nieprawidłowa opcja modelu.')
    return c


def ffmpeg(args):
    p = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
                        "-nostdin", *map(str, args)], capture_output=True)
    if p.returncode:
        raise RuntimeError(p.stderr.decode("utf-8", errors="replace")[-5000:])


def probe(path):
    reader = imageio_ffmpeg.read_frames(str(path))
    try:
        meta = next(reader)
    finally:
        reader.close()
    if not meta.get("duration") or meta["duration"] <= 0:
        raise ValueError("Brak dodatniego czasu trwania: " + str(path))
    info = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-nostdin', '-i', str(path)], capture_output=True)
    match = re.search(r'creation_time\s*:\s*(\S+)', info.stderr.decode('utf-8', errors='replace'))
    if match:
        try:
            review_features.timestamp(match.group(1))
            meta['recorded_at'] = match.group(1)
        except ValueError:
            pass
    return meta


def classify_segment(classifier, path, start, end):
    images = []
    for fraction in (.2, .5, .8):
        result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-v', 'error', '-nostdin',
                                 '-ss', str(start + (end-start)*fraction), '-i', str(path),
                                 '-frames:v', '1', '-vf', 'scale=224:224', '-f', 'rawvideo',
                                 '-pix_fmt', 'rgb24', '-'], capture_output=True, check=True)
        if len(result.stdout) != 224*224*3:
            raise ValueError('Nie udało się odczytać klatki do klasyfikacji.')
        images.append(np.frombuffer(result.stdout, np.uint8).reshape(224, 224, 3))
    return classifier.classify(images)


def trim(project, candidate_id, edge, delta):
    project = Path(project)
    state = load_project(project)
    candidate = review_features.changed_range(state, candidate_id, edge, delta)
    source = next(f for f in state['files'] if f['id'] == candidate['file_id'])
    stat = Path(source['path']).stat()
    if stat.st_size != source['size'] or stat.st_mtime_ns != source['mtime_ns']:
        raise ValueError('Oryginał zmienił się od analizy.')
    suffix = secrets.token_hex(6)
    candidate['preview'] = f'review/{candidate_id}-{suffix}.mp4'
    candidate['thumbnail'] = f'review/{candidate_id}-{suffix}.jpg'
    render_segment(source['path'], project/candidate['preview'], candidate['start'],
                   max(1, math.floor((candidate['end']-candidate['start'])*25)), 25, 640, 360, source['audio'], 25)
    ffmpeg(['-y', '-i', project/candidate['preview'], '-frames:v', 1, project/candidate['thumbnail']])
    samples = read_json(project/(candidate['file_id']+'-metrics.json'))
    fps = state['config']['sample_fps']
    window = samples[int(candidate['start']*fps):max(int(candidate['start']*fps)+1, math.ceil(candidate['end']*fps))]
    if window:
        candidate['score'], candidate['metrics'] = window_score(window, state['config']['profile'])
    candidate['category_note'] = 'Kategorie dotyczą propozycji przed ręczną korektą.'
    edits_path = project/'edits.json'
    edits = read_json(edits_path) if edits_path.exists() else {}
    edits[candidate_id] = {k: candidate[k] for k in ('start','end','preview','thumbnail','score','metrics','category_note')}
    save_json(edits_path, edits)


def frame_features(gray, previous):
    sharp = float(cv2.Laplacian(gray, cv2.CV_32F).var())
    dark = float(np.mean(gray < 12))
    white = float(np.mean(gray > 248))
    contrast = float(gray.std())
    shift = (0., 0.)
    residual = motion = 0.
    if previous is not None:
        (dx, dy), response = cv2.phaseCorrelate(previous.astype(np.float32), gray.astype(np.float32))
        if response > .12 and abs(dx) < 80 and abs(dy) < 45:
            shift = (dx, dy)
            motion = min(1., math.hypot(dx, dy) / 35)
            aligned = cv2.warpAffine(previous, np.float32([[1, 0, dx], [0, 1, dy]]),
                                     (320, 180), borderMode=cv2.BORDER_REFLECT)
            residual = float(np.mean(cv2.absdiff(gray[20:-20, 30:-30], aligned[20:-20, 30:-30]))) / 255
        else:
            residual = float(np.mean(cv2.absdiff(gray, previous))) / 255
    # White snow alone is weak evidence of a defect, unlike near-black frames.
    detail = min(1., math.log1p(sharp) / math.log(501))
    quality = np.clip(.65 * detail + .35 * min(1., contrast / 45) - .7 * dark - .12 * white, 0, 1)
    return dict(quality=float(quality), sharpness=sharp, dark=dark, white=white,
                residual=residual, motion=motion, dx=shift[0], dy=shift[1])


def window_score(features, profile):
    q = float(np.median([f["quality"] for f in features]))
    m = float(np.median([f["motion"] for f in features]))
    novelty = min(1., float(np.median([f["residual"] for f in features])) / .16)
    vectors = np.array([[f["dx"], f["dy"]] for f in features])
    shake = min(1., float(np.mean(np.linalg.norm(np.diff(vectors, axis=0), axis=1))) / 30) if len(vectors) > 1 else 0.
    # No linear "more movement = better" rule. Still scenery keeps a strong baseline.
    activity = math.exp(-((m - .28) / .35) ** 2)
    shake_weight = .12 if profile == "pov" else .22 if profile == "phone" else .17
    score = 100 * q * max(0., .78 + .12 * activity + .10 * novelty - shake_weight * shake)
    return round(score, 2), dict(quality=round(q, 3), activity=round(activity, 3),
                               change=round(novelty, 3), shake=round(shake, 3))


def pick_windows(samples, duration, c):
    """Segment usable footage at changes of visual activity, then pad and rank.

    Activity locates boundaries only: it does not override quality or the score.
    Legacy window_seconds/stride_seconds are accepted but no longer used.
    """
    if not samples or duration < .5:
        return []
    c = DEFAULT | c
    fps = c["sample_fps"]
    n = len(samples)
    quality = np.array([x["quality"] for x in samples])
    change = np.array([x["residual"] for x in samples])
    # Median filter removes isolated spikes without smoothing a brief event away.
    change = np.array([np.median(change[max(0, i-1):min(n, i+2)]) for i in range(n)])
    usable = quality >= .3
    baseline = float(np.median(change[usable])) if usable.any() else 0.
    high = max(.07, baseline + .035)
    low = high * .65
    labels = []
    active = False
    for i in range(n):
        if not usable[i]:
            active = False
            labels.append(-1)
            continue
        active = change[i] >= (low if active else high)
        labels.append(1 if active else 0)
    # Bridge short pauses within an event, never bridge unusable frames.
    gap_limit = int(c["merge_gap_seconds"] * fps)
    for i in range(1, n):
        if labels[i] != 0 or labels[i-1] != 1:
            continue
        j = i
        while j < n and labels[j] == 0:
            j += 1
        if j < n and labels[j] == 1 and j-i <= gap_limit:
            labels[i:j] = [1] * (j-i)
    runs = []
    a = 0
    for b in range(1, n+1):
        if b == n or labels[b] != labels[a]:
            if labels[a] >= 0:
                runs.append((a / fps, min(duration, b / fps), labels[a]))
            a = b
    # Keep short event boundaries. Expand by context, then resolve shared context
    # at the midpoint so two candidates never repeat the same source frames.
    spans = []
    for a, b, kind in runs:
        if b <= a:
            continue
        start = max(0., a - c["pre_roll_seconds"])
        end = min(duration, b + c["post_roll_seconds"])
        missing = max(0., c["min_clip_seconds"] - (end-start))
        start = max(0., start-missing/2)
        end = min(duration, max(end, start+c["min_clip_seconds"]))
        start = max(0., min(start, end-c["min_clip_seconds"]))
        spans.append([start, end, kind])
    for i in range(1, len(spans)):
        previous, current = spans[i-1], spans[i]
        if previous[1] > current[0]:
            cut = current[0] if current[2] == 1 else previous[1] if previous[2] == 1 else (previous[1]+current[0])/2
            previous[1] = cut
            current[0] = cut
    # A very short intervening run can collapse after context allocation.
    # Remove it, then resolve overlap between its now-adjacent neighbours.
    spans = [span for span in spans if span[1] > span[0]]
    for i in range(1, len(spans)):
        if spans[i-1][1] > spans[i][0]:
            cut = (spans[i-1][1] + spans[i][0])/2
            spans[i-1][1] = cut
            spans[i][0] = cut
    candidates = []
    for start, end, kind in spans:
        # Long passages split near a low-change point, not every fixed 6 seconds.
        while end-start > c["max_clip_seconds"]:
            lower = start+c["max_clip_seconds"]*.6
            upper = min(start+c["max_clip_seconds"], end-c["min_clip_seconds"])
            indices = list(range(math.ceil(lower*fps), min(n, math.floor(upper*fps)+1)))
            cut = min(indices, key=lambda j: (change[j], abs(j/fps-upper))) / fps if indices else upper
            candidates.append((start, cut, kind))
            start = cut
        if end > start:
            candidates.append((start, end, kind))
    scored = []
    for start, end, kind in candidates:
        # A whole short source is retained; sub-minimum slivers are not proposed.
        if end-start < min(duration, c["min_clip_seconds"])-.001:
            continue
        window = samples[int(start*fps):max(int(start*fps)+1, math.ceil(end*fps))]
        core = [sample for sample in window if sample["quality"] >= .3]
        if not core:
            continue
        score, metrics = window_score(core, c["profile"])
        score = round(score * (.7 + .3 * len(core)/len(window)), 2)
        if score >= c["min_score"]:
            scored.append(dict(start=round(start, 3), end=round(end, 3), score=score,
                               metrics=metrics, reason="zmiana obrazu" if kind else "spójny fragment"))
    selected = sorted(scored, key=lambda x: (-x["score"], x["start"]))[:c["candidates_per_file"]]
    return sorted(selected, key=lambda x: x["start"])


def render_segment(source, dest, start, frames, fps, width, height, audio, crf=18):
    # FFmpeg honors display rotation; fit with black padding, never crop.
    vf = (f"setpts=PTS-STARTPTS,fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease:"
          f"force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
          "tpad=stop_mode=clone:stop_duration=1")
    args = ["-y", "-ss", f"{start:.6f}", "-i", source]
    if not audio:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    args += ["-map", "0:v:0", "-map", "0:a:0" if audio else "1:a:0", "-vf", vf,
             "-af", "asetpts=PTS-STARTPTS,aresample=48000:async=1:first_pts=0,apad",
             "-frames:v", frames, "-t", f"{frames / fps:.9f}",
             "-c:v", "libx264", "-preset", "fast", "-crf", crf, "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-map_metadata", "-1",
             "-metadata:s:v:0", "rotate=0", "-movflags", "+faststart", dest]
    ffmpeg(args)


def analyze(input_dir, project, c):
    classifier = semantic.Classifier() if c.get('semantic_enabled') else None
    if c.get('categories') and not classifier:
        raise ValueError('Wybór kategorii wymaga włączenia modelu.')
    input_dir, project = Path(input_dir).resolve(), Path(project).resolve()
    if not input_dir.is_dir():
        raise ValueError("Nie istnieje folder wejściowy")
    if input_dir == project or input_dir.is_relative_to(project) or project.is_relative_to(input_dir):
        raise ValueError("Folder projektu i wejścia muszą być oddzielne (bez zagnieżdżania)")
    if project.exists():
        raise ValueError("Projekt już istnieje. Użyj nowego folderu; decyzje nie będą nadpisane.")
    files = sorted(p for p in input_dir.rglob("*") if p.suffix.lower() in (".mp4", ".mov") and p.is_file())
    # Multiple filesystem aliases of one source must not create duplicate proposals.
    unique = {}
    for source in files:
        stat = source.stat()
        key = (stat.st_dev, stat.st_ino) if stat.st_ino else str(source.resolve()).casefold()
        unique.setdefault(key, source)
    files = list(unique.values())
    if not files:
        raise ValueError("Brak plików MP4/MOV")
    project.mkdir(parents=True)
    (project / "review").mkdir()
    state = dict(version=1, config=c, files=[], candidates=[], errors=[], status="analyzing")
    save_json(project / "project.json", state)
    for source in files:
        print("Analiza:", source.name, flush=True)
        try:
            stat = source.stat()
            meta = probe(source)
            fid = hashlib.sha256(str(source).encode()).hexdigest()[:16]
            item = dict(id=fid, path=str(source), size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                        duration=meta["duration"], fps=meta["fps"],
                        audio=bool(meta.get("audio_codec")), metadata=meta)
            item['recorded_at'] = meta.get('recorded_at')
            samples, previous = [], None
            reader = imageio_ffmpeg.read_frames(str(source), pix_fmt="gray", bits_per_pixel=8,
                                                output_params=["-vf", f"fps={c['sample_fps']},scale=320:180"])
            try:
                next(reader)
                for raw in reader:
                    gray = np.frombuffer(raw, np.uint8).reshape(180, 320)
                    samples.append(frame_features(gray, previous))
                    previous = gray
            finally:
                reader.close()
            # Full sample metrics remain available for inspecting the heuristic.
            save_json(project / (fid + "-metrics.json"), samples)
            state["files"].append(item)
            proposed = pick_windows(samples, meta['duration'], c | {'candidates_per_file': 100000} if classifier else c)
            eligible = []
            for win in proposed:
                win['categories'], win['category_scores'] = classify_segment(classifier, source, win['start'], win['end']) if classifier else (['unknown'], {})
                if not c.get('categories') or set(win['categories']) & set(c['categories']):
                    eligible.append(win)
            eligible = sorted(sorted(eligible, key=lambda w: -w['score'])[:c['candidates_per_file']], key=lambda w: w['start'])
            for index, win in enumerate(eligible):
                cid = f"{fid}-{index:03d}"
                win.update(id=cid, file_id=fid, name=source.name, decision="pending",
                           preview=f"review/{cid}.mp4", thumbnail=f"review/{cid}.jpg")
                n = max(1, math.floor((win["end"] - win["start"]) * 25))
                render_segment(source, project / win["preview"], win["start"], n, 25, 640, 360, item["audio"], 25)
                ffmpeg(["-y", "-ss", (win["end"] - win["start"]) / 2,
                        "-i", project / win["preview"], "-frames:v", 1, project / win["thumbnail"]])
                state["candidates"].append(win)
        except Exception as exc:
            state["errors"].append(dict(file=str(source), error=str(exc)))
            print("Błąd:", source.name, exc, flush=True)
        save_json(project / "project.json", state)
    state["status"] = "ready"
    save_json(project / "project.json", state)
    print(f"Gotowe: {len(state['candidates'])} kandydatów; błędy: {len(state['errors'])}", flush=True)
    return state


def reselect(old_project, project, c):
    """Rebuild proposals from saved metrics; do not resume scanning source folder."""
    old_project, project = Path(old_project).resolve(), Path(project).resolve()
    old = read_json(old_project / "project.json")
    if project.exists():
        raise ValueError("Projekt docelowy już istnieje; wybierz nowy folder")
    if project.is_relative_to(old_project) or old_project.is_relative_to(project):
        raise ValueError("Projekty muszą znajdować się w oddzielnych folderach")
    c = c.copy()
    c["sample_fps"] = old["config"]["sample_fps"]
    (project / "review").mkdir(parents=True)
    state = dict(version=2, config=c, files=[], candidates=[], errors=[], status="analyzing",
                 based_on=str(old_project), selection="adaptive")
    save_json(project / "project.json", state)
    for source in old["files"]:
        print("Nowe propozycje:", Path(source["path"]).name, flush=True)
        try:
            stat = Path(source["path"]).stat()
            if stat.st_size != source["size"] or stat.st_mtime_ns != source["mtime_ns"]:
                raise ValueError("Oryginał zmienił się od analizy")
            samples = read_json(old_project / (source["id"] + "-metrics.json"))
            save_json(project / (source["id"] + "-metrics.json"), samples)
            for index, win in enumerate(pick_windows(samples, source["duration"], c)):
                cid = f"{source['id']}-adaptive-{index:03d}"
                win.update(id=cid, file_id=source["id"], name=Path(source["path"]).name,
                           decision="pending", preview=f"review/{cid}.mp4", thumbnail=f"review/{cid}.jpg")
                count = max(1, math.floor((win["end"]-win["start"])*25))
                render_segment(source["path"], project / win["preview"], win["start"], count,
                               25, 640, 360, source["audio"], 25)
                ffmpeg(["-y", "-ss", (win["end"]-win["start"])/2, "-i", project / win["preview"],
                        "-frames:v", 1, project / win["thumbnail"]])
                state["candidates"].append(win)
            state["files"].append(source)
        except Exception as exc:
            state["errors"].append(dict(file=source["path"], error=str(exc)))
        save_json(project / "project.json", state)
    state["status"] = "ready"
    save_json(project / "project.json", state)
    print(f"Gotowe: {len(state['candidates'])} propozycji, błędy: {len(state['errors'])}", flush=True)
    return state


def node(parent, tag, value=None, **attrs):
    el = ET.SubElement(parent, tag, attrs)
    if value is not None:
        el.text = str(value)
    return el


def source_xml_metadata(source):
    """Read nominal stream timing, not the average fps reported for VFR media."""
    info = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", source["path"]], capture_output=True)
    details = info.stderr.decode("utf-8", errors="replace")
    video = next((line for line in details.splitlines() if "Video:" in line), "")
    tbr = re.search(r"([0-9.]+)\s+tbr\b", video)
    capture = re.search(r"com\.android\.capture\.fps\s*:\s*([0-9.]+)", details)
    reported = float(tbr.group(1)) if tbr else float(capture.group(1)) if capture else source["fps"]
    source["xml_fps"] = xml_fps(reported)
    source["xml_rate_origin"] = "stream_tbr" if tbr else "capture_metadata" if capture else "average_fallback"
    source["xml_timing_approximate"] = abs(source["fps"] - source["xml_fps"]) > .02
    if source["audio"]:
        audio = next((line for line in details.splitlines() if "Audio:" in line), "")
        sample_rate = re.search(r"(\d+)\s+Hz", audio)
        source["audio_sample_rate"] = int(sample_rate.group(1)) if sample_rate else 48000
        if ", mono," in audio:
            source["audio_channels"] = 1
        elif ", stereo," in audio:
            source["audio_channels"] = 2
        else:
            raise ValueError("XML obsługuje obecnie oryginalny dźwięk mono lub stereo: " + source["path"])
    return source


def xml_fps(fps):
    """Map rounded/average phone rates to the nearest XML nominal rate.

    A nominal XML rate cannot describe variable frame timing exactly. Keep the
    approximation bounded (0.1%) and use the SAME rate for metadata and offsets.
    """
    fps = float(fps)
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("XML: nieprawidłowy klatkaż źródła")
    choices = [float(round(fps))] + [n * 1000 / 1001 for n in (24, 30, 60, 120)]
    nearest = min(choices, key=lambda value: abs(value - fps))
    if nearest <= 0 or abs(nearest - fps) / nearest > .001:
        raise ValueError("XML: niestandardowy klatkaż źródła: " + str(fps))
    return nearest


def rate(parent, fps):
    r = node(parent, "rate")
    fps = xml_fps(fps)
    nominal = round(fps)
    ntsc = nominal in (24, 30, 60, 120) and abs(fps - nominal * 1000 / 1001) < .000001
    node(r, "timebase", nominal)
    node(r, "ntsc", "TRUE" if ntsc else "FALSE")


def video_format(parent, c):
    rate(parent, c["timeline_fps"])
    node(parent, "width", c["width"])
    node(parent, "height", c["height"])
    node(parent, "anamorphic", "FALSE")
    node(parent, "pixelaspectratio", "square")
    node(parent, "fielddominance", "none")


def write_xml(path, clips, c):
    """Resolve-compatible FCP7 XML: one native audio stream per clip.

    Follow Resolve's own Windows export: localhost URI, explicit source
    timecode and a shared file declaration for linked video/audio clipitems.
    Stereo remains stereo; trackindex selects the stream, not its channels.
    """
    root = ET.Element("xmeml", version="5")
    seq = node(root, "sequence")
    node(seq, "name", "LPX AI video review — zatwierdzone")
    node(seq, "duration", sum(x["frames"] for x in clips))
    rate(seq, c["timeline_fps"])
    node(seq, "in", -1)
    node(seq, "out", -1)
    def timecode(parent, fps):
        tc = node(parent, "timecode")
        node(tc, "string", "00:00:00:00")
        node(tc, "frame", 0)
        node(tc, "displayformat", "NDF")
        rate(tc, fps)
    timecode(seq, c["timeline_fps"])
    media = node(seq, "media")
    video = node(media, "video")
    vt = node(video, "track")
    video_format(node(node(video, "format"), "samplecharacteristics"), c)
    audio = node(media, "audio")
    at = node(audio, "track")
    position = 0
    for idx, clip in enumerate(clips, 1):
        source = clip["source"]
        fps = xml_fps(source.get("xml_fps", source["fps"]))
        timeline_fps = c['timeline_fps']
        start = round(clip["source_start_seconds"] * timeline_fps)
        end = start + clip['frames']
        safe_duration = source.get('video_duration', source['duration'])
        duration = source.get('video_frame_count', math.floor(safe_duration * fps + 1e-7)) if source.get('constant_rate', True) else math.floor(safe_duration * fps + 1e-7)
        ids = [f"v-{idx}"] + ([f"a-{idx}"] if source["audio"] else [])
        for kind, track in enumerate([vt, at][:len(ids)]):
            ci = node(track, "clipitem", id=ids[kind])
            node(ci, "name", clip["name"])
            # Clip timing belongs to the sequence grid; the file keeps its native rate.
            node(ci, "duration", math.floor(safe_duration * timeline_fps + 1e-7))
            rate(ci, timeline_fps)
            node(ci, "start", position)
            node(ci, "end", position + clip["frames"])
            node(ci, "enabled", "TRUE")
            node(ci, "in", start)
            node(ci, "out", end)
            f = node(ci, "file", id=f"file-{idx}")
            if kind == 0:
                node(f, "duration", duration)
                rate(f, fps)
                node(f, "name", Path(source["path"]).name)
                uri = Path(source["path"]).resolve().as_uri()
                if uri.startswith("file:///"):
                    uri = "file://localhost/" + uri[len("file:///"):]
                node(f, "pathurl", uri)
                timecode(f, fps)
                fm = node(f, "media")
                fv = node(fm, "video")
                node(fv, "duration", duration)
                width, height = source.get("metadata", {}).get("source_size", source.get("metadata", {}).get("size", (c["width"], c["height"])))
                sc = node(fv, "samplecharacteristics")
                node(sc, "width", width)
                node(sc, "height", height)
                if source["audio"]:
                    node(node(fm, "audio"), "channelcount", source.get("audio_channels", 2))
            else:
                st = node(ci, "sourcetrack")
                node(st, "mediatype", "audio")
                node(st, "trackindex", 1)
                for name, effect_id, parameter_name, parameter_id, value in [
                    ("Audio Levels", "audiolevels", "Level", "level", 1),
                    ("Audio Pan", "audiopan", "Pan", "pan", 0)]:
                    filt = node(ci, "filter")
                    node(filt, "enabled", "TRUE")
                    node(filt, "start", position)
                    node(filt, "end", position + clip["frames"])
                    effect = node(filt, "effect")
                    for tag, val in [("name", name), ("effectid", effect_id),
                                     ("effecttype", effect_id), ("mediatype", "audio"),
                                     ("effectcategory", effect_id)]:
                        node(effect, tag, val)
                    param = node(effect, "parameter")
                    node(param, "name", parameter_name)
                    node(param, "parameterid", parameter_id)
                    node(param, "value", value)
            for ref in ids:
                link = node(ci, "link")
                node(link, "linkclipref", ref)
                if kind and ref == ids[0]:
                    node(link, "mediatype", "video")
            node(ci, "comments")
        position += clip["frames"]
    for track in (vt, at):
        node(track, "enabled", "TRUE")
        node(track, "locked", "FALSE")
    ET.indent(root)
    Path(path).write_bytes(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + ET.tostring(root, encoding="utf-8"))


def export(project, state=None):
    project = Path(project).resolve()
    state = state or load_project(project)
    c = state["config"].copy()
    selected = [x for x in state["candidates"] if x["decision"] == "accepted"]
    if not selected:
        raise ValueError("Najpierw zaakceptuj przynajmniej jeden fragment")
    files = {x["id"]: x for x in state["files"]}
    review_features.validate_ranges(state, selected)
    selected = review_features.ordered(state, selected)
    fps = c["timeline_fps"]
    checked = set()
    for x in selected:
        src = files[x["file_id"]]
        stat = Path(src["path"]).stat()
        if stat.st_size != src["size"] or stat.st_mtime_ns != src["mtime_ns"]:
            raise ValueError("Plik źródłowy zmienił się od analizy: " + src["path"])
        if x["file_id"] not in checked:
            source_xml_metadata(src)
            timing = media_timing.video_timing(src['path'])
            src['video_frame_count'] = timing['frame_count']
            src['constant_rate'] = timing['constant_rate']
            src['video_duration'] = min(src['duration'], timing['duration'])
            if timing['constant_rate']:
                src['video_duration'] = min(src['video_duration'], timing['frame_count']/src['xml_fps'])
            checked.add(x["file_id"])
    native_rates = {files[fid]['xml_fps'] for fid in checked}
    if len(native_rates) == 1:
        c['timeline_fps'] = native_rates.pop()
    fps = c['timeline_fps']
    # An exclusive lock also protects separate CLI processes.
    lock = project / "export.lock"
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    try:
        from datetime import datetime
        out = project / ("export-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2))
        (out / "media").mkdir(parents=True)
        clips = []
        for i, x in enumerate(selected, 1):
            save_json(project / "export-progress.json", dict(message=f"Przygotowanie podglądu: klip {i} z {len(selected)}. Proszę czekać…", completed=i-1, total=len(selected)))
            src = files[x["file_id"]]
            first, last = media_timing.plan_range(x['start'], x['end'], src['video_duration'], fps)
            n = last - first
            source_start = first / fps
            dest = out / "media" / f"clip-{i:04d}.mp4"
            print("Eksport:", x["name"], x["start"], flush=True)
            render_segment(src["path"], dest, source_start, n, fps, c["width"], c["height"], src["audio"])
            clips.append(dict(path=str(dest), name=x["name"], frames=n, candidate_id=x["id"],
                              source=src, original=src["path"], source_start_seconds=source_start,
                              source_end_seconds=last / fps, timeline_start_frame=sum(z["frames"] for z in clips)))
        save_json(project / "export-progress.json", dict(message="Zapisywanie XML z odwołaniami do oryginałów…"))
        write_xml(out / "timeline.xml", clips, c)
        save_json(out / "manifest.json", dict(config=c, clips=clips, decisions=selected))
        # Relative generated names avoid concat quoting problems for arbitrary source paths.
        (out / "concat.txt").write_text("".join(f"file 'media/{Path(x['path']).name}'\n" for x in clips), encoding="utf-8")
        save_json(project / "export-progress.json", dict(message="Łączenie podglądu. Program pracuje — proszę czekać…"))
        ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", out / "concat.txt",
                "-vf", "scale=960:540", "-c:v", "libx264", "-preset", "fast", "-crf", "24",
                "-c:a", "aac", "-movflags", "+faststart", out / "preview.mp4"])
        save_json(out / "complete.json", {"status": "complete", "xml_media": "originals", "frames": sum(x["frames"] for x in clips)})
        return out
    finally:
        lock.unlink(missing_ok=True)


def serve(project, port=8765):
    project = Path(project).resolve()
    state = load_project(project)
    token = secrets.token_urlsafe(32)
    mutex = threading.Lock()
    job = {"status": "idle"}
    assets = {"/" + x[k]: project / x[k] for x in state["candidates"] for k in ("preview", "thumbnail")}

    class Handler(BaseHTTPRequestHandler):
        def refresh_analysis(self):
            current = load_project(project)
            state.clear()
            state.update(current)
            assets.update({"/" + x[k]: project / x[k] for x in state["candidates"]
                           for k in ("preview", "thumbnail")})

        def send(self, code, data, content_type="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                html = Path(__file__).with_name("review.html").read_text(encoding="utf-8").replace("__TOKEN__", token)
                self.send(200, html.encode(), "text/html; charset=utf-8")
            elif path == "/api/state":
                with mutex:
                    self.refresh_analysis()
                    data = dict(candidates=state["candidates"], errors=state["errors"], job=job,
                                analysis_status=state.get("status", "ready"), analyzed_files=len(state["files"]),
                                max_seconds=state["config"]["max_export_seconds"])
                    self.send(200, json.dumps(data, ensure_ascii=False).encode())
            elif path in assets:
                self.send_file(assets[path])
            else:
                self.send(404, b'{}')

        def send_file(self, path):
            size = path.stat().st_size
            start, end, status = 0, size - 1, 200
            requested = self.headers.get("Range")
            if requested:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
                if not match or not any(match.groups()):
                    self.send(416, b'{}'); return
                a, b = match.groups()
                if a:
                    start, end = int(a), min(int(b), end) if b else end
                else:
                    start = max(0, size - int(b))
                if start > end or start >= size:
                    self.send(416, b'{}'); return
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", "video/mp4" if path.suffix == ".mp4" else "image/jpeg")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            try:
                with path.open("rb") as stream:
                    stream.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        chunk = stream.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_POST(self):
            # Browser cross-origin requests cannot mutate state. No CORS opt-in.
            if self.headers.get("X-Review-Token") != token:
                self.send(403, b'{}'); return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length < 10000:
                    raise ValueError("Nieprawidłowy rozmiar żądania")
                data = json.loads(self.rfile.read(length))
                with mutex:
                    self.refresh_analysis()
                    if job["status"] == "running":
                        raise ValueError("Trwa eksport. Poczekaj na zakończenie.")
                    if self.path == "/api/decision":
                        if data["decision"] not in ("accepted", "rejected", "pending"):
                            raise ValueError("Nieprawidłowa decyzja")
                        candidate = next(x for x in state["candidates"] if x["id"] == data["id"])
                        record_decision(project, data["id"], data["decision"])
                        candidate["decision"] = data["decision"]
                    elif self.path == "/api/export":
                        if state.get("status") == "analyzing":
                            raise ValueError("Eksport będzie dostępny po zakończeniu analizy. Możesz już zapisywać decyzje.")
                        snapshot = copy.deepcopy(state)
                        job.clear(); job.update(status="running")
                        def worker():
                            try:
                                out = export(project, snapshot)
                                with mutex:
                                    assets["/export-preview.mp4"] = out / "preview.mp4"
                                    job.update(status="complete", folder=str(out), preview="/export-preview.mp4")
                            except Exception as exc:
                                with mutex:
                                    job.update(status="error", error=str(exc))
                        threading.Thread(target=worker, daemon=True).start()
                    else:
                        self.send(404, b'{}'); return
                self.send(200, b'{"ok":true}')
            except (ValueError, KeyError, StopIteration, OSError) as exc:
                self.send(400, json.dumps({"error": str(exc)}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Otwórz http://127.0.0.1:{server.server_port} — Ctrl+C kończy serwer.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="LPX AI video review — lokalny montaż z akceptacją")
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("analyze")
    a.add_argument("--input", required=True)
    a.add_argument("--project", required=True)
    a.add_argument("--config")
    r = sub.add_parser("reselect")
    r.add_argument("--from-project", required=True)
    r.add_argument("--project", required=True)
    r.add_argument("--config")
    s = sub.add_parser("serve")
    s.add_argument("--project", required=True)
    s.add_argument("--port", type=int, default=8765)
    e = sub.add_parser("export")
    e.add_argument("--project", required=True)
    t = sub.add_parser('trim')
    t.add_argument('--project', required=True)
    t.add_argument('--id', required=True)
    t.add_argument('--edge', choices=['start', 'end'], required=True)
    t.add_argument('--delta', type=float, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'trim':
            trim(args.project, args.id, args.edge, args.delta)
            return 0
        if args.command == "analyze":
            result = analyze(args.input, args.project, config(args.config))
            return 2 if result["errors"] else 0
        if args.command == "reselect":
            result = reselect(args.from_project, args.project, config(args.config))
            return 2 if result["errors"] else 0
        if args.command == "serve":
            serve(args.project, args.port)
        else:
            print(export(args.project))
        return 0
    except Exception as exc:
        print("BŁĄD:", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
