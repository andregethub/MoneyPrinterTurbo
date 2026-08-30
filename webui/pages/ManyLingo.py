import os
import sys
from uuid import uuid4

import streamlit as st

root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
if root_dir in sys.path:
    sys.path.remove(root_dir)
sys.path.insert(0, root_dir)

from app.config import config  # noqa: E402
from app.models import const  # noqa: E402
from app.models.schema import (  # noqa: E402
    ManyLingoItem,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
)
from app.services import state as sm  # noqa: E402
from app.services import upload_post  # noqa: E402
from app.services import webui_task  # noqa: E402
from app.services import manylingo_queue as ml_queue  # noqa: E402
from app.services.manylingo import (  # noqa: E402
    build_narration,
    generate_manylingo_items,
    items_to_editor_text,
)

DEFAULT_WORDS = """house
living room
bedroom"""

DEFAULT_ITEMS = """house | This house is big. | Esta casa es grande. | large house exterior
living room | We watch TV in the living room. | Vemos televisión en la sala. | family watching television in living room
bedroom | The bedroom is quiet. | El dormitorio es tranquilo. | cozy bedroom interior"""

# Keep the CTA evergreen so videos created before launch remain useful after launch.
DEFAULT_CTA = """Aprende inglés con ManyLingo
manylingo.com"""

DEFAULT_VOCAB_IMPORT = """house | A1 | Home
living room | A1 | Home
bedroom | A1 | Home
kitchen | A1 | Home
bathroom | A1 | Home"""


def parse_items(raw_text: str):
    items = []
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
        search_terms.append(item.search_term or item.word)

    if not items:
        raise ValueError("Adicione pelo menos uma palavra.")

    return items, build_narration(items), search_terms


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
        # Keep the first pass through all vocabulary scenes short. The ManyLingo
        # post-render step then stretches each scene to its spoken block duration.
        video_clip_duration=2,
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


def _submit_group(
    *,
    group: dict,
    translation_language: str,
    watermark: str,
    cta: str,
    cta_duration: float,
    voice_name: str,
    video_source: str,
):
    items = generate_manylingo_items(
        group["words"],
        translation_language=translation_language,
    )
    narration = build_narration(items)
    search_terms = [item.search_term or item.word for item in items]
    subject = f"English vocabulary: {group['topic']} ({group['level']})"
    params = build_params(
        subject=subject,
        items=items,
        narration=narration,
        search_terms=search_terms,
        watermark=watermark,
        cta=cta,
        cta_duration=cta_duration,
        voice_name=voice_name,
        video_source=video_source,
    )
    task_id = str(uuid4())
    webui_task.submit_generation(
        task_id=task_id,
        params=params,
        capture_logs=True,
    )
    ml_queue.create_job(
        task_id=task_id,
        group=group,
        items=[item.model_dump() for item in items],
        subject=subject,
        narration=narration,
    )
    return task_id


st.set_page_config(
    page_title="ManyLingo Video Mode",
    page_icon="🌎",
    layout="wide",
)

if "manylingo_editor_text" not in st.session_state:
    st.session_state["manylingo_editor_text"] = DEFAULT_ITEMS

st.title("ManyLingo Video Mode")
st.caption(
    "Fábrica de aquisição: banco de vocabulário → IA → TTS → cenas → vídeo → revisão → publicação."
)

settings = ml_queue.get_settings()
stats = ml_queue.vocabulary_stats()

with st.container(border=True):
    st.subheader("0. Automação")
    col_stats_1, col_stats_2, col_stats_3 = st.columns(3)
    col_stats_1.metric("Palavras no banco", stats["total"])
    col_stats_2.metric("Ainda não usadas", stats["unused"])
    col_stats_3.metric("Vídeos na fila", len(ml_queue.list_jobs(limit=200)))

    with st.expander("Importar / atualizar banco de vocabulário", expanded=stats["total"] == 0):
        st.caption(
            "Faça esta importação uma vez. Formato: palavra | nível | tema. Depois a máquina escolhe sozinha o próximo conteúdo."
        )
        vocab_import = st.text_area(
            "Vocabulário",
            value=DEFAULT_VOCAB_IMPORT if stats["total"] == 0 else "",
            height=160,
            placeholder="house | A1 | Home\nwater | A1 | Food and drink",
        )
        default_level = st.selectbox(
            "Nível padrão para linhas sem nível",
            ["A1", "A2", "B1", "B2", "C1", "C2"],
            index=0,
        )
        default_topic = st.text_input("Tema padrão", value="General")
        if st.button("Importar vocabulário", use_container_width=True):
            try:
                result = ml_queue.import_vocabulary(
                    vocab_import,
                    default_level=default_level,
                    default_topic=default_topic,
                )
            except Exception as exc:
                st.error(f"Não foi possível importar: {exc}")
            else:
                st.success(
                    f"Banco atualizado: {result['added']} nova(s), {result['updated']} atualizada(s), {result['total']} no total."
                )
                st.rerun()

    review_before_publish = st.toggle(
        "Revisar antes de publicar",
        value=bool(settings.get("review_before_publish", True)),
        help="Enquanto ligado, gere e assista aos vídeos antes de usar o botão Publicar.",
    )
    if review_before_publish != bool(settings.get("review_before_publish", True)):
        ml_queue.set_settings(review_before_publish=review_before_publish)
        settings = ml_queue.get_settings()

    upload_auto = bool(upload_post.upload_post_service.auto_upload)
    if review_before_publish and upload_auto:
        st.error(
            "Proteção de revisão: upload_post_auto_upload está ligado no config.toml. Desative-o antes de gerar um lote para impedir publicação automática antes da sua aprovação."
        )
    elif review_before_publish:
        st.info("Modo seguro: os vídeos ficam aguardando sua revisão antes da publicação.")
    elif upload_auto:
        st.success("Modo automático: o Upload-Post está habilitado para publicar após a geração.")
    else:
        st.warning(
            "A revisão foi desligada, mas upload_post_auto_upload também está desligado. Os vídeos serão gerados, porém não serão publicados automaticamente."
        )

    if stats["total"]:
        levels = ml_queue.available_levels()
        col1, col2, col3 = st.columns(3)
        batch_level = col1.selectbox("Nível do lote", levels, index=0)
        words_per_video = col2.number_input(
            "Palavras por vídeo", min_value=1, max_value=10, value=3, step=1
        )
        batch_count = col3.number_input(
            "Quantidade de vídeos", min_value=1, max_value=20, value=5, step=1
        )

        translation_language_auto = st.selectbox(
            "Idioma das traduções do lote",
            ["Spanish", "Portuguese", "French", "German", "Italian"],
            index=0,
            key="auto_translation_language",
        )

with st.container(border=True):
    st.subheader("1. Palavras — modo manual/teste")
    subject = st.text_input("Tema", value="English vocabulary: Home")
    words_text = st.text_area(
        "Uma palavra ou expressão por linha",
        value=DEFAULT_WORDS,
        height=120,
        help="Use este campo para testes. No lote automático, a máquina escolhe as palavras do banco.",
    )
    translation_language = st.selectbox(
        "Idioma da tradução",
        options=["Spanish", "Portuguese", "French", "German", "Italian"],
        index=0,
    )

    if st.button("✨ Criar frases e cenas com IA", use_container_width=True):
        try:
            with st.spinner("Criando frases, traduções e termos visuais..."):
                generated_items = generate_manylingo_items(
                    words_text,
                    translation_language=translation_language,
                )
        except Exception as exc:
            st.error(f"Não foi possível gerar o conteúdo com IA: {exc}")
        else:
            st.session_state["manylingo_editor_text"] = items_to_editor_text(
                generated_items
            )
            st.success(
                f"Conteúdo criado para {len(generated_items)} palavra(s). Revise abaixo antes de gerar o vídeo."
            )

with st.container(border=True):
    st.subheader("2. Revisar conteúdo manual")
    items_text = st.text_area(
        "Palavra | frase em inglês | tradução | termo visual",
        key="manylingo_editor_text",
        height=200,
        help="Você pode editar qualquer frase, tradução ou termo de busca antes da geração.",
    )
    st.code(
        "house | This house is big. | Esta casa es grande. | large house exterior",
        language=None,
    )

with st.container(border=True):
    st.subheader("3. Marca e CTA")
    watermark = st.text_input("Marca d'água", value="manylingo.com")
    cta = st.text_area("CTA final", value=DEFAULT_CTA, height=90)
    cta_duration = st.slider(
        "Duração do CTA (segundos)", min_value=0.0, max_value=6.0, value=2.5, step=0.5
    )
    st.caption(
        "A CTA é atemporal: não menciona lista de espera. O manylingo.com decide se mostra pré-lançamento ou o app pronto."
    )

with st.container(border=True):
    st.subheader("4. Gerar")
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
        "O vídeo é 9:16. Cada palavra recebe sua própria cena em ordem; depois a cena é ajustada à duração estimada da fala daquele bloco."
    )

    manual_col, batch_col = st.columns(2)
    with manual_col:
        if st.button("Gerar 1 vídeo manual", type="secondary", use_container_width=True):
            try:
                items, narration, search_terms = parse_items(items_text)
            except ValueError as exc:
                st.error(str(exc))
            else:
                if not voice_name.strip():
                    st.error("Configure uma voz TTS antes de gerar.")
                elif review_before_publish and upload_auto:
                    st.error("Desative upload_post_auto_upload para usar revisão manual com segurança.")
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

    with batch_col:
        batch_disabled = (
            stats["total"] == 0
            or not voice_name.strip()
            or (review_before_publish and upload_auto)
        )
        if st.button(
            f"⚙️ Gerar lote automático ({int(batch_count) if stats['total'] else 0})",
            type="primary",
            use_container_width=True,
            disabled=batch_disabled,
        ):
            try:
                groups = ml_queue.plan_word_groups(
                    level=batch_level,
                    video_count=int(batch_count),
                    words_per_video=int(words_per_video),
                )
                if not groups:
                    raise ValueError("O banco não encontrou palavras para este lote.")

                created = []
                progress = st.progress(0, text="Planejando o lote...")
                for index, group in enumerate(groups, start=1):
                    progress.progress(
                        int((index - 1) / len(groups) * 100),
                        text=(
                            f"Preparando vídeo {index}/{len(groups)} — "
                            f"{group['topic']}: {', '.join(group['words'])}"
                        ),
                    )
                    task_id = _submit_group(
                        group=group,
                        translation_language=translation_language_auto,
                        watermark=watermark,
                        cta=cta,
                        cta_duration=cta_duration,
                        voice_name=voice_name.strip(),
                        video_source=video_source,
                    )
                    created.append(task_id)
                progress.progress(100, text="Lote enviado para a fila de geração.")
                st.success(
                    f"{len(created)} vídeo(s) enviados. O MoneyPrinterTurbo processará a fila um por vez."
                )
            except Exception as exc:
                st.error(f"Não foi possível criar o lote automático: {exc}")

current_task_id = str(st.session_state.get("manylingo_task_id", "") or "").strip()
if current_task_id:
    st.divider()
    st.subheader("Status do teste manual")

    @st.fragment(run_every="2s")
    def render_current_manylingo_task():
        _render_task_status(current_task_id)

    render_current_manylingo_task()

st.divider()
st.subheader("Fila ManyLingo")
st.caption("A fila é persistente em storage/manylingo/automation.json e sobrevive a reinícios.")


@st.fragment(run_every="3s")
def render_manylingo_queue():
    jobs = ml_queue.refresh_jobs()[:20]
    if not jobs:
        st.info("Nenhum vídeo automático foi criado ainda.")
        return

    for job in jobs:
        status = str(job.get("status") or "queued")
        words = ", ".join(job.get("words") or [])
        title = f"{job.get('level', 'A1')} · {job.get('topic', 'General')} · {words}"
        with st.container(border=True):
            st.markdown(f"**{title}**")
            cols = st.columns([1, 1, 2])
            cols[0].write(f"Status: **{status}**")
            cols[1].write(f"Task: `{str(job.get('task_id') or '')[:8]}`")

            task = None
            try:
                task = sm.state.get_task(str(job.get("task_id") or ""))
            except Exception:
                pass
            if task and status in {"queued", "generating"}:
                progress_value = max(0, min(100, int(task.get("progress", 0) or 0)))
                st.progress(progress_value, text=f"Geração: {progress_value}%")

            if job.get("error"):
                st.error(str(job["error"]))

            for video_path in job.get("video_paths") or []:
                if os.path.exists(video_path):
                    st.video(video_path)

            if status == "review":
                action_cols = st.columns(2)
                if action_cols[0].button(
                    "✅ Aprovar e publicar",
                    key=f"publish-{job['id']}",
                    use_container_width=True,
                ):
                    try:
                        ml_queue.publish_job_async(job["id"])
                    except Exception as exc:
                        st.error(f"Não foi possível publicar: {exc}")
                    else:
                        st.rerun()

                if action_cols[1].button(
                    "🔁 Refazer vídeo",
                    key=f"redo-{job['id']}",
                    use_container_width=True,
                ):
                    try:
                        item_models = [ManyLingoItem(**item) for item in job.get("items") or []]
                        narration = build_narration(item_models)
                        search_terms = [item.search_term or item.word for item in item_models]
                        params = build_params(
                            subject=str(job.get("subject") or "ManyLingo vocabulary"),
                            items=item_models,
                            narration=narration,
                            search_terms=search_terms,
                            watermark=watermark,
                            cta=cta,
                            cta_duration=cta_duration,
                            voice_name=voice_name.strip(),
                            video_source=video_source,
                        )
                        new_task_id = str(uuid4())
                        webui_task.submit_generation(
                            task_id=new_task_id,
                            params=params,
                            capture_logs=True,
                        )
                        ml_queue.create_job(
                            task_id=new_task_id,
                            group={
                                "level": job.get("level"),
                                "topic": job.get("topic"),
                                "words": job.get("words") or [],
                                "vocabulary_ids": job.get("vocabulary_ids") or [],
                            },
                            items=[item.model_dump() for item in item_models],
                            subject=str(job.get("subject") or "ManyLingo vocabulary"),
                            narration=narration,
                        )
                        ml_queue.set_job_status(job["id"], "failed", error="Substituído por uma nova geração.")
                    except Exception as exc:
                        st.error(f"Não foi possível refazer: {exc}")
                    else:
                        st.rerun()

            elif status == "publishing":
                st.info("Publicando via Upload-Post...")
            elif status == "published":
                st.success("Publicado.")


render_manylingo_queue()
