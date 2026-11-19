"""Shared source-colour handling and deterministic QuPath display defaults."""
from collections.abc import Mapping
from typing import Any
import numpy as np


def channel_color(channel: Mapping[str, Any], index: int) -> str:
    """Use NGFF RGB, or the same default as QuPath 0.7 ImageChannel.

    Declared white is a colour, not missing metadata. The fallback follows
    ImageChannel.getDefaultChannelColor, including Java's float32 HSB rounding.
    """
    if 'color' in channel:
        color = channel['color']
        if not isinstance(color, str) or len(color) != 6 or any(c not in '0123456789abcdefABCDEF' for c in color):
            raise ValueError('Image channel colours must contain six hexadecimal RGB digits.')
        return '#' + color.lower()
    index %= 360
    initial = ('ff0000', '00ff00', '0000ff', 'ffe000', '00e0e0', 'ff00e0')
    if index < len(initial):
        return '#' + initial[index]
    f = np.float32
    hue = f((index * 128) % 360) / f(360)
    saturation = brightness = f(1) - f(index // 10) / f(20)
    h = f(hue * f(6)); fraction = f(h - f(int(h)))
    p = f(brightness * (f(1) - saturation))
    q = f(brightness * (f(1) - f(saturation * fraction)))
    t = f(brightness * (f(1) - f(saturation * (f(1) - fraction))))
    rgb = ((brightness, t, p), (q, brightness, p), (p, brightness, t),
           (p, q, brightness), (t, p, brightness), (brightness, p, q))[int(h)]
    red, green, blue = (int(f(value * f(255)) + f(.5)) for value in rgb)
    return f'#{((red << 16) | (green << 8) | blue) & 0xffffff:06x}'

