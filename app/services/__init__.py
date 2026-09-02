"""Service package bootstrap hooks."""

from app.services.manylingo import install_video_patch
from app.services.manylingo_elevenlabs import install_elevenlabs_timing_patch
from app.services.manylingo_quality import install_quality_patch
from app.services.manylingo_timing import install_timing_patch

install_quality_patch()
install_video_patch()
install_elevenlabs_timing_patch()
install_timing_patch()
