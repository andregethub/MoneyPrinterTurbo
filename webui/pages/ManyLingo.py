import os
import sys
from uuid import uuid4

import streamlit as st

root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
if root_dir in sys.path:
    sys.path.remove(root_dir)
sys.path.insert(0, root_dir)

from app.config import config
from app.models import const
from app.models.schema import ManyLingoItem, VideoAspect, VideoConcatMode, VideoParams
from app.services import state as sm
from app.services import webui_task

DEFAULT_ITEMS = """house | This house is big. | Esta casa es grande. | large house exterior
living room | We watch TV in the living room. | Vemos televisión en la sala. | family watching television in living room
bedroom | The bedroom is quiet. | El dormitorio es tranquilo. | cozy bedroom interior"""

DEFAULT_CTA = """Aprende inglés todos los días
manylingo.com
Comenta MANYLINGO para recibir el enlace"""


def parse_items(raw_text: str):
    items = []
    narration_parts = []
    search_terms = []

    for line_number, raw_line in enumerate(str(raw_text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        parts = [part.strip() for part in line.split("|", 3)]
        if len(parts) < 3:
            raise ValueError(
                f"Linha {line_number}: use palavra | frase em inglês | tradução | termo de busca"
            )

        word, sentence, translation = parts[:3]
        search_term = parts[3] if len(parts) == 4 else word
        if not word:
            raise ValueError(f"Linha {line_number}: a palavra não pode ficar vazia")

        item = ManyLingoItem(
            word=word,
            sentence=sentence,
            translation=translation,
            search_term=search_term or word,
        )
        items.append(item)

        narration_parts.append(word)
        if sentence:
            narration_parts.append(sentence)
        search_terms.append(item.search_term or item.word)

    if not items:
        raise ValueError("Adicione pelo menos uma palavra.")

    narration = ". ".join(part.rstrip(".?!") for part in narration_parts if part).strip()
    if narration:
        narration += "."
    return items, narration, search_terms


def _configured_voice_name() -> str:
    return str(config.ui.get("voice_name", "") or "").strip()


def _configured_font_name() -> str:
    return str(
        config.ui.get("font_name", "MicrosoftYaHeiBold.ttc")
        or "MicrosoftYaHeiBold.ttc"
    ).strip()


def _configured_video_source() -> str:
    source = str(config.app.get("video_source", "pexels") or "pexels").strip()
    return source if source in {"pexels", "pixabay"} else "pexels"


def build_params(
    *,
    subject: str,
    items,
    narration: str,
    search_terms,
    watermark: str,
    cta: str,
    cta_duration: float,
    voice_name: str,
    video_source: str,
) -> VideoParams:
    return VideoParams(
        video_subject=subject.strip() or "ManyLingo vocabulary",
        video_script=narration,
        video_terms=list(search_terms),
        content_mode="manylingo",
        manylingo_items=list(items),
        manylingo_watermark=watermark.strip(),
        manylingo_cta=cta.strip(),
        manylingo_cta_duration=float(cta_duration),
        video_aspect=VideoAspect.portrait.value,
        video_concat_mode=VideoConcatMode.sequential.value,
        match_materials_to_script=True,
        video_clip_duration=4,
        video_count=1,
        video_source=video_source,
        video_language="en-US",
        voice_name=voice_name,
        voice_volume=float(config.ui.get("voice_volume", 1.0) or 1.0),
        voice_rate=float(config.ui.get("voice_rate", 1.0) or 1.0),
        bgm_type=str(config.ui.get("bgm_type", "random") or ""),
        bgm_volume=float(config.ui.get("bgm_volume", 0.2) or 0.0),
        subtitle_enabled=False,
        font_name=_configured_font_name(),
        n_threads=2,
    )


def _render_task_status(task_id: str):
    try:
        task = sm.state.get_task(task_id)
    except Exception as exc:
        st.error(f"Não foi possível consultar a tarefa: {exc}")
        return

    if not task:
        st.info("Preparando a tarefa...")
        return

    state = task.get("state")
    progress = max(0, min(100, int(task.get("progress", 0) or 0)))

    if state == const.TASK_STATE_FAILED:
        st.error(f"Falha ao gerar vídeo: {task.get('error') or 'erro desconhecido'}")
        return

    if state != const.TASK_STATE_COMPLETE:
        st.info("Gerando vídeo ManyLingo...")
        st.progress(progress, text=f"Progresso: {progress}%")
        return

    videos = task.get("videos") or []
    if not videos:
        st.warning("A tarefa terminou, mas nenhum vídeo final foi encontrado.")
        return

    st.success("Vídeo ManyLingo concluído.")
    for video_path in videos:
        st.video(video_path)


st.set_page_config(
    page_title="ManyLingo Video Mode",
    page_icon="🌎",
    layout="wide",
)

st.title("ManyLingo Video Mode")
st.caption(
    "Modo separado para criar Shorts/Reels/TikToks de vocabulário sem alterar o fluxo normal do MoneyPrinterTurbo."
)

with st.container(border=True):
    st.subheader("Conteúdo")
    subject = st.text_input("Tema", value="English vocabulary: Home")
    items_text = st.text_area(
        "Palavras e frases",
        value=DEFAULT_ITEMS,
        height=180,
        help=(
            "Uma linha por item: palavra | frase em inglês | tradução em espanhol | termo de busca do vídeo"
        ),
    )
    st.code(
        "house | This house is big. | Esta casa es grande. | large house exterior",
        language=None,
    )

with st.container(border=True):
    st.subheader("Marca e CTA")
    watermark = st.text_input("Marca d'água", value="manylingo.com")
    cta = st.text_area("CTA final", value=DEFAULT_CTA, height=110)
    cta_duration = st.slider(
        "Duração do CTA (segundos)", min_value=0.0, max_value=6.0, value=2.5, step=0.5
    )

with st.container(border=True):
    st.subheader("Geração")
    configured_voice = _configured_voice_name()
    voice_name = st.text_input(
        "Voz TTS",
        value=configured_voice,
        help="Usa por padrão a voz que já está salva nas configurações do MoneyPrinterTurbo.",
    )
    source_options = ["pexels", "pixabay"]
    configured_source = _configured_video_source()
    video_source = st.selectbox(
        "Fonte dos vídeos",
        options=source_options,
        index=source_options.index(configured_source),
    )
    st.caption(
        "O vídeo é sempre vertical 9:16, usa os termos de busca na mesma ordem do vocabulário e desativa a legenda normal para não duplicar o texto didático."
    )

    if st.button("Gerar vídeo ManyLingo", type="primary", use_container_width=True):
        try:
            items, narration, search_terms = parse_items(items_text)
        except ValueError as exc:
            st.error(str(exc))
        else:
            if not voice_name.strip():
                st.error(
                    "Selecione/configure uma voz TTS na página principal do MoneyPrinterTurbo ou informe o nome da voz aqui."
                )
            else:
                params = build_params(
                    subject=subject,
                    items=items,
                    narration=narration,
                    search_terms=search_terms,
                    watermark=watermark,
                    cta=cta,
                    cta_duration=cta_duration,
                    voice_name=voice_name.strip(),
                    video_source=video_source,
                )
                task_id = str(uuid4())
                try:
                    webui_task.submit_generation(
                        task_id=task_id,
                        params=params,
                        capture_logs=True,
                    )
                except Exception as exc:
                    st.error(f"Não foi possível iniciar a geração: {exc}")
                else:
                    st.session_state["manylingo_task_id"] = task_id
                    st.success(f"Tarefa criada: {task_id}")

current_task_id = str(st.session_state.get("manylingo_task_id", "") or "").strip()
if current_task_id:
    st.divider()
    st.subheader("Status")

    @st.fragment(run_every="2s")
    def render_current_manylingo_task():
        _render_task_status(current_task_id)

    render_current_manylingo_task()
