"""Import yt-dlp only when it is actually used.

yt-dlp costs ~30MB of RAM just to import. YouTube now resolves through the
local resolver first, so most of the time yt-dlp is never needed at all.
"""


class _LazyYtDlp:
    _module = None

    def __getattr__(self, name):
        if _LazyYtDlp._module is None:
            import yt_dlp as module

            _LazyYtDlp._module = module
        return getattr(_LazyYtDlp._module, name)


yt_dlp = _LazyYtDlp()
