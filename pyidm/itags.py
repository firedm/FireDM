# -*- coding: utf-8 -*-
"""
    This module contains a lookup table of YouTube's itag values.

    special thanks to nficano for this itags properties.
    https://github.com/nficano/pytube.git
"""

PROGRESSIVE_VIDEO = {
    5: "240p",
    6: "270p",
    13: "144p",
    17: "144p",
    18: "360p",
    22: "720p",
    34: "360p",
    35: "480p",
    36: "240p",
    37: "1080p",
    38: "3072p",
    43: "360p",
    44: "480p",
    45: "720p",
    46: "1080p",
    59: "480p",
    78: "480p",
    82: "360p",
    83: "480p",
    84: "720p",
    85: "1080p",
    91: "144p",
    92: "240p",
    93: "360p",
    94: "480p",
    95: "720p",
    96: "1080p",
    100: "360p",
    101: "480p",
    102: "720p",
    132: "240p",
    151: "720p",
    300: "720p",
    301: "1080"
}

DASH_VIDEO = {
    # DASH Video
    133: "240p", 
    134: "360p", 
    135: "480p", 
    136: "720p", 
    137: "1080p",
    138: "2160p",
    160: "144p", 
    167: "360p", 
    168: "480p", 
    169: "720p", 
    170: "1080p",
    212: "480p", 
    218: "480p", 
    219: "480p", 
    242: "240p", 
    243: "360p", 
    244: "480p", 
    245: "480p", 
    246: "480p", 
    247: "720p", 
    248: "1080p",
    264: "1440p",
    266: "2160p",
    271: "1440p",
    272: "4320p",
    278: "144p", 
    298: "720p", 
    299: "1080p",
    302: "720p", 
    303: "1080p",
    308: "1440p",
    313: "2160p",
    315: "2160p",
    330: "144p", 
    331: "240p", 
    332: "360p", 
    333: "480p", 
    334: "720p", 
    335: "1080p",
    336: "1440p",
    337: "2160p",
    394: "144p", 
    395: "240p", 
    396: "360p", 
    397: "480p", 
    398: "720p", 
    399: "1080p",
    400: "1440p",
    401: "2160p",
    402: "4320p",
    571: "4320p",
}

DASH_AUDIO = {
    # DASH Audio
    139: "48kbps",
    140: "128kbps",
    141: "256kbps",
    171: "128kbps",
    172: "256kbps",
    249: "50kbps", 
    250: "70kbps", 
    251: "160kbps",
    256: "192kbps",
    258: "384kbps",
}

ITAGS = {
    **PROGRESSIVE_VIDEO,
    **DASH_VIDEO,
    **DASH_AUDIO,
}


def itag_res(itag: int) -> str:
    '''
    returns audio bitrate or video pixel ratio or 0 in case unknown itag

    :rtype: str
    '''
    itag = int(itag)
    res = ITAGS.get(itag, "0")
    return res