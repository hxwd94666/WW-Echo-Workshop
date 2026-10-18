# 在声骸详情区域识别冻结套装图标，不承担资料更新。
from dataclasses import dataclass
import hashlib
from echo_workshop.capture.viewport import EchoViewport
from echo_workshop.resources.sonata import checked_png


@dataclass(frozen=True)
class IconObservation:
    ident: str = ''
    name: str = ''
    score: float = 0
    runner_up: float = 0
    bounds: tuple = ()
    state: str = 'unknown'
    version: str = ''


class SonataMatcher:
    def __init__(self, snapshot, *, minimum=0.80, margin=0.12):
        if not 0 < minimum <= 1 or not 0 < margin <= 1:
            raise ValueError('Invalid icon match thresholds')
        self.snapshot, self.minimum, self.margin = snapshot, minimum, margin
        self._templates = None
        self._sized = {}
        self._last_match = None
        self.last_search = 'unknown'

    def match(self, frame, layout, viewport=None, *, scan=False):
        import cv2
        import numpy as np

        self.last_search = 'unknown'
        if (
            frame is None
            or frame.ndim != 3
            or frame.shape[2] < 3
            or layout not in ('bag', 'display')
        ):
            return IconObservation(version=self.snapshot.version)
        h, w = frame.shape[:2]
        area = EchoViewport.checked(viewport, w, h)
        if area.narrow:
            return IconObservation(version=self.snapshot.version)
        if self._last_match is not None:
            old_layout, old_viewport, previous, pixels, mask = self._last_match
            if old_layout == layout and old_viewport == area.bounds:
                x, y, sw, sh = previous.bounds
                current = frame[y : y + sh, x : x + sw, :3]
                # Reuse only exact opaque-icon pixels, not an old item/name or
                # a tolerance-based visual guess. Each new record still has
                # its own fresh OCR and capture stability/identity checks.
                if current.shape == pixels.shape and np.array_equal(current[mask], pixels[mask]):
                    self.last_search = 'exact_cache'
                    return previous
        dx, dy = area.offset('right')
        ref = area.reference_width
        # The selected detail header only; never scan inventory grid icons.
        limits = (0.69, 0.16, 0.82, 0.24) if layout == 'bag' else (0.88, 0.12, 0.978, 0.17)
        left, top = round(dx + ref * limits[0]), round(dy + area.height * limits[1])
        right, bottom = round(dx + ref * limits[2]), round(dy + area.height * limits[3])
        roi = frame[top:bottom, left:right, :3].astype(np.float32)
        if roi.size == 0:
            return IconObservation(version=self.snapshot.version)
        if self._templates is None:
            templates = {}
            for ident, icon in self.snapshot.icons.items():
                body = checked_png(icon.path.read_bytes())
                if hashlib.sha256(body).hexdigest() != icon.sha256:
                    raise ValueError('Sonata template changed after snapshot')
                image = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_UNCHANGED)
                if image is None or image.ndim != 3:
                    raise ValueError('Sonata template decode failed')
                if image.shape[2] == 3:
                    image = np.dstack((image, np.full(image.shape[:2], 255, np.uint8)))
                templates[ident] = image
            self._templates = templates
        sizes = tuple(range(max(10, round(area.height * 0.016)), round(area.height * 0.032) + 1))
        if sizes not in self._sized:
            # A matcher belongs to a fixed snapshot; bound resized-cache growth
            # when users repeatedly resize the game window.
            if len(self._sized) >= 4:
                self._sized.clear()
            self._sized[sizes] = {
                ident: tuple(cv2.resize(image, (s, s), interpolation=cv2.INTER_AREA) for s in sizes)
                for ident, image in self._templates.items()
            }
        # Locate a few circle candidates first. Searching every pixel of the
        # header with 37 x many sizes otherwise dominates automatic intake.
        gray = cv2.cvtColor(roi.astype(np.uint8), cv2.COLOR_BGR2GRAY)
        if float(gray.std()) < 1:
            return IconObservation(version=self.snapshot.version)
        circles = cv2.HoughCircles(
            cv2.GaussianBlur(gray, (3, 3), 0),
            cv2.HOUGH_GRADIENT,
            1,
            max(6, area.height * 0.007),
            param1=100,
            param2=max(7, area.height * 0.0065),
            minRadius=max(4, round(area.height * 0.007)),
            maxRadius=round(area.height * 0.016),
        )
        searches = []
        if circles is not None:
            for cx, cy, radius in sorted(circles[0], key=lambda c: float(c[0]), reverse=True)[:8]:
                radius = float(radius) + 7
                x, y = max(0, round(cx - radius)), max(0, round(cy - radius))
                right, bottom = (
                    min(roi.shape[1], round(cx + radius)),
                    min(roi.shape[0], round(cy + radius)),
                )
                searches.append((roi[y:bottom, x:right], left + x, top + y))

        def rank(regions, allowed_sizes=None):
            ranking = []
            for ident, variants in self._sized[sizes].items():
                best, bounds = -1, ()
                for image in variants:
                    size = image.shape[0]
                    if allowed_sizes is not None and size not in allowed_sizes:
                        continue
                    mask = (image[:, :, 3] > 220).astype(np.uint8) * 255
                    if np.count_nonzero(mask) < 20:
                        continue
                    template = image[:, :, :3].astype(np.float32)
                    for cropped, x, y in regions:
                        if size > cropped.shape[0] or size > cropped.shape[1]:
                            continue
                        scores = cv2.matchTemplate(
                            cropped, template, cv2.TM_CCOEFF_NORMED, mask=mask
                        )
                        scores = np.nan_to_num(scores, nan=-1, posinf=-1, neginf=-1)
                        _, value, _, location = cv2.minMaxLoc(scores)
                        if value > best:
                            best, bounds = value, (x + location[0], y + location[1], size, size)
                ranking.append((best, ident, bounds))
            return sorted(ranking, reverse=True)

        ranking = []
        if scan and layout == 'bag':
            # Only the fixed automatic bag header uses this prediction. Its
            # circle's nominal asset canvas is ~0.0264 of content height; retain
            # a small scale neighborhood and the same confidence/margin gate.
            # Any ambiguity falls back to the existing bounded multi-size path.
            center = (round(dx + ref * 0.738), round(dy + area.height * 0.197))
            radius = round(area.height * 0.026)
            fx, fy = max(left, center[0] - radius), max(top, center[1] - radius)
            fr, fb = (
                min(left + roi.shape[1], center[0] + radius),
                min(top + roi.shape[0], center[1] + radius),
            )
            expected = round(area.height * 0.0264)
            ranking = rank(
                ((frame[fy:fb, fx:fr, :3].astype(np.float32), fx, fy),),
                set(range(expected - 2, expected + 3)),
            )
            if (
                ranking
                and ranking[0][0] >= self.minimum
                and (len(ranking) == 1 or ranking[0][0] - ranking[1][0] >= self.margin)
            ):
                self.last_search = 'scan_predicted'
            else:
                ranking = []
        if not ranking:
            self.last_search = 'bounded_fallback' if scan and layout == 'bag' else 'ordinary'
            ranking = rank(searches) if searches else []
        if (
            not ranking
            or ranking[0][0] < self.minimum
            or (len(ranking) > 1 and ranking[0][0] - ranking[1][0] < self.margin)
        ):
            ranking = rank(((roi, left, top),))
        if not ranking:
            return IconObservation(version=self.snapshot.version)
        score, ident, bounds = ranking[0]
        runner = ranking[1][0] if len(ranking) > 1 else 0
        if score < self.minimum or score - runner < self.margin:
            return IconObservation(
                score=score, runner_up=runner, bounds=bounds, version=self.snapshot.version
            )
        result = IconObservation(
            ident,
            self.snapshot.icons[ident].name,
            score,
            runner,
            bounds,
            'matched',
            self.snapshot.version,
        )
        x, y, sw, sh = bounds
        template = cv2.resize(self._templates[ident], (sw, sh), interpolation=cv2.INTER_AREA)
        self._last_match = (
            layout,
            area.bounds,
            result,
            frame[y : y + sh, x : x + sw, :3].copy(),
            template[:, :, 3] > 220,
        )
        return result
