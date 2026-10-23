"""Read video sample timing from MP4/MOV without decoding the movie."""
import struct
from fractions import Fraction
from pathlib import Path


def video_timing(path):
    with Path(path).open('rb') as stream:
        def boxes(start, end):
            while start + 8 <= end:
                stream.seek(start)
                size, kind = struct.unpack('>I4s', stream.read(8))
                header = 8
                if size == 1:
                    size = struct.unpack('>Q', stream.read(8))[0]
                    header = 16
                elif size == 0:
                    size = end - start
                if size < header or start + size > end:
                    raise ValueError('Nieprawidłowa struktura MP4/MOV.')
                yield kind, start + header, start + size
                start += size

        def child(bounds, name):
            return next(((a, b) for kind, a, b in boxes(*bounds) if kind == name), None)

        moov = child((0, Path(path).stat().st_size), b'moov')
        if not moov:
            raise ValueError('Brak metadanych moov w nagraniu.')
        for kind, a, b in boxes(*moov):
            if kind != b'trak': continue
            mdia = child((a, b), b'mdia')
            if not mdia: continue
            hdlr = child(mdia, b'hdlr')
            if not hdlr: continue
            stream.seek(hdlr[0] + 8)
            if stream.read(4) != b'vide': continue
            mdhd = child(mdia, b'mdhd')
            stream.seek(mdhd[0])
            version = stream.read(1)[0]
            stream.seek(mdhd[0] + (20 if version == 1 else 12))
            scale = struct.unpack('>I', stream.read(4))[0]
            minf = child(mdia, b'minf')
            stbl = child(minf, b'stbl') if minf else None
            stts = child(stbl, b'stts') if stbl else None
            if not stts or not scale: break
            stream.seek(stts[0] + 4)
            entries = struct.unpack('>I', stream.read(4))[0]
            if entries > (stts[1] - stts[0] - 8) // 8:
                raise ValueError('Uszkodzona tabela czasu klatek.')
            count = ticks = 0
            deltas = set()
            for _ in range(entries):
                n, delta = struct.unpack('>II', stream.read(8))
                count += n
                ticks += n * delta
                if n: deltas.add(delta)
            if count and ticks:
                return dict(frame_count=count, duration=float(Fraction(ticks, scale)),
                            constant_rate=len(deltas) == 1, timing_origin='mp4_stts')
        raise ValueError('Brak tabeli czasu klatek; wymagany zwykły MP4/MOV (bez fragmentacji).')


def plan_range(start, end, duration, timeline_fps):
    """One shared frame grid for preview, XML and audio; never exceed source EOF."""
    import math
    first = max(0, math.ceil(start * timeline_fps - 1e-7))
    last = math.floor(min(end, duration) * timeline_fps + 1e-7)
    if last <= first:
        raise ValueError('Po dopasowaniu klatkażu fragment nie zawiera pełnej klatki osi czasu.')
    return first, last
