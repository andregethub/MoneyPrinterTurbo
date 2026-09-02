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
from app.models.schema import ManyLingoItem, VideoAspect, VideoConcatMode, VideoParams  # noqa: E402
from app.services import manylingo_distribution as distribution  # noqa: E402
from app.services import manylingo_queue as ml_queue  # noqa: E402
from app.services import state as sm, upload_post, voice as voice_service, webui_task  # noqa: E402
from app.services.manylingo import build_narration, generate_manylingo_items, items_to_editor_text  # noqa: E402

DEFAULT_WORDS = "house\nliving room\nbedroom"
DEFAULT_ITEMS = """house | This house is big. | Esta casa es grande. | large suburban house exterior
living room | We watch TV in the living room. | Vemos televisión en la sala. | family watching television in living room
bedroom | The bedroom is quiet. | El dormitorio es tranquilo. | cozy bedroom interior"""
DEFAULT_CTA = "Aprende inglés con ManyLingo\nmanylingo.com"
DEFAULT_VOCAB_IMPORT = "house | A1 | Home\nliving room | A1 | Home\nbedroom | A1 | Home"
DEFAULT_PLAN_IMPORT = """# video_id | nível | tema | ordem | palavra | frase | tradução | termo visual
A1-0001 | A1 | Home | 1 | house | This house is very big. | Esta casa es muy grande. | large suburban house exterior
A1-0001 | A1 | Home | 2 | bedroom | My bedroom is upstairs. | Mi dormitorio está arriba. | cozy bedroom interior
A1-0001 | A1 | Home | 3 | bathroom | The bathroom is next to my room. | El baño está al lado de mi habitación. | modern bathroom interior"""


def parse_items(raw_text: str):
    items, terms = [], []
    for number, raw in enumerate(str(raw_text or "").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("|", 3)]
        if len(parts) < 3:
            raise ValueError(f"Linha {number}: use palavra | frase | tradução | termo visual")
        word, sentence, translation = parts[:3]
        term = parts[3] if len(parts) == 4 else word
        item = ManyLingoItem(
            word=word,
            sentence=sentence,
            translation=translation,
            search_term=term or word,
        )
        items.append(item)
        terms.append(item.search_term or item.word)
    if not items:
        raise ValueError("Adicione pelo menos uma palavra.")
    return items, build_narration(items), terms


def _voice():
    configured = str(config.ui.get("voice_name", "") or "").strip()
    if configured.lower().startswith("en-"):
        return configured
    return "en-US-GuyNeural"


@st.cache_data(ttl=300, show_spinner=False)
def _elevenlabs_voices():
    api_key = voice_service.get_elevenlabs_api_key()
    if not api_key:
        return []
    return voice_service.get_elevenlabs_voices(api_key)


def _elevenlabs_voice_label(value: str) -> str:
    parts = str(value or "").split(":", 2)
    if len(parts) >= 3 and parts[2].strip():
        return parts[2].strip()
    if len(parts) >= 2:
        return parts[1].strip()
    return str(value or "")


def _font():
    return str(
        config.ui.get("font_name", "MicrosoftYaHeiBold.ttc")
        or "MicrosoftYaHeiBold.ttc"
    ).strip()


def _source():
    source = str(config.app.get("video_source", "pexels") or "pexels").strip()
    return source if source in {"pexels", "pixabay"} else "pexels"


def _ensure_group_available(group: dict) -> None:
    group_id = str(group.get("group_id") or "").strip()
    if not group_id:
        return
    for job in ml_queue.refresh_jobs():
        if (
            str(job.get("group_id") or "") == group_id
            and str(job.get("status") or "") in {"queued", "generating"}
        ):
            raise ValueError(
                f"O grupo {group_id} já está em geração. Aguarde a conclusão ou remova a tarefa interrompida."
            )


def build_params(
    *,
    subject,
    items,
    narration,
    search_terms,
    watermark,
    cta,
    cta_duration,
    voice_name,
    video_source,
    aspect=VideoAspect.portrait.value,
):
    return VideoParams(
        video_subject=subject.strip() or "ManyLingo vocabulary",
        video_script=narration,
        video_terms=list(search_terms),
        content_mode="manylingo",
        manylingo_items=list(items),
        manylingo_watermark=watermark.strip(),
        manylingo_cta=cta.strip(),
        manylingo_cta_duration=float(cta_duration),
        video_aspect=aspect,
        video_concat_mode=VideoConcatMode.sequential.value,
        match_materials_to_script=True,
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
        font_name=_font(),
        n_threads=2,
    )


def _submit_group(
    *, group, translation_language, watermark, cta, cta_duration, voice_name, video_source
):
    _ensure_group_available(group)
    if group.get("items"):
        items = [ManyLingoItem(**item) for item in group["items"]]
    else:
        items = generate_manylingo_items(
            group["words"], translation_language=translation_language
        )
    narration = build_narration(items)
    terms = [item.search_term or item.word for item in items]
    subject = f"English vocabulary: {group['topic']} ({group['level']})"
    params = build_params(
        subject=subject,
        items=items,
        narration=narration,
        search_terms=terms,
        watermark=watermark,
        cta=cta,
        cta_duration=cta_duration,
        voice_name=voice_name,
        video_source=video_source,
        aspect=VideoAspect.portrait.value,
    )
    task_id = str(uuid4())
    ml_queue.create_job(
        task_id=task_id,
        group=group,
        items=[item.model_dump() for item in items],
        subject=subject,
        narration=narration,
    )
    try:
        webui_task.submit_generation(task_id=task_id, params=params, capture_logs=True)
    except Exception as exc:
        matching = next(
            (job for job in ml_queue.list_jobs(limit=200) if job.get("task_id") == task_id),
            None,
        )
        if matching:
            ml_queue.set_job_status(matching["id"], "failed", error=str(exc))
        raise
    return task_id


def _submit_horizontal(
    *, group, watermark, cta, cta_duration, voice_name, video_source
):
    _ensure_group_available(group)
    items = [ManyLingoItem(**item) for item in group["items"]]
    narration = build_narration(items)
    terms = [item.search_term or item.word for item in items]
    subject = (
        f"{len(items)} English Words About {group['topic']} | "
        f"{group['level']} English Vocabulary"
    )
    params = build_params(
        subject=subject,
        items=items,
        narration=narration,
        search_terms=terms,
        watermark=watermark,
        cta=cta,
        cta_duration=cta_duration,
        voice_name=voice_name,
        video_source=video_source,
        aspect=VideoAspect.landscape.value,
    )
    task_id = str(uuid4())
    job = ml_queue.create_job(
        task_id=task_id,
        group={**group, "vocabulary_ids": []},
        items=[item.model_dump() for item in items],
        subject=subject,
        narration=narration,
    )
    distribution.mark_horizontal_job(job["id"], group)
    try:
        webui_task.submit_generation(task_id=task_id, params=params, capture_logs=True)
    except Exception as exc:
        ml_queue.set_job_status(job["id"], "failed", error=str(exc))
        raise
    return task_id


def _task_status(task_id):
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
        st.progress(progress, text=f"Gerando vídeo: {progress}%")
        return
    for path in task.get("videos") or []:
        st.video(path)


st.set_page_config(page_title="ManyLingo Video Mode", page_icon="🌎", layout="wide")
if "manylingo_editor_text" not in st.session_state:
    st.session_state["manylingo_editor_text"] = DEFAULT_ITEMS

ml_queue.refresh_jobs()

st.title("ManyLingo Video Mode")
st.caption(
    "Currículo pré-planejado → TTS → cenas → vídeo → revisão → distribuição. "
    "Os Shorts e os vídeos 16:9 usam o mesmo banco de conteúdo."
)
settings = ml_queue.get_settings()
stats = ml_queue.vocabulary_stats()
vertical_destinations, distribution_warnings = distribution.vertical_platforms()

with st.container(border=True):
    st.subheader("0. Currículo e automação")
    all_jobs = ml_queue.list_jobs(limit=200)
    failed_jobs = [job for job in all_jobs if job.get("status") == "failed"]
    a, b, c, d = st.columns(4)
    a.metric("Palavras", stats["total"])
    b.metric("Não usadas", stats["unused"])
    c.metric("Grupos prontos", stats.get("preplanned_groups", 0))
    d.metric("Vídeos na fila", len(all_jobs))

    if failed_jobs:
        st.warning(
            f"Há {len(failed_jobs)} tarefa(s) interrompida(s) ou com falha. "
            "Você pode limpá-las sem apagar o currículo."
        )
        if st.button(
            f"Limpar tarefas interrompidas ({len(failed_jobs)})",
            use_container_width=True,
        ):
            for failed_job in failed_jobs:
                ml_queue.remove_job(failed_job["id"])
            st.success("Tarefas interrompidas removidas. O currículo foi preservado.")
            st.rerun()

    with st.expander(
        "Importar currículo pré-planejado",
        expanded=stats.get("preplanned_groups", 0) == 0,
    ):
        st.caption(
            "Formato fixo. Palavras do mesmo video_id sempre ficam juntas. "
            "Frase, espanhol e termo visual ficam salvos e não precisam ser recriados por IA."
        )
        plan_text = st.text_area(
            "Plano de vídeos",
            value=DEFAULT_PLAN_IMPORT if stats["total"] == 0 else "",
            height=220,
        )
        if st.button("Importar plano fixo", use_container_width=True):
            try:
                result = ml_queue.import_preplanned_curriculum(plan_text)
            except Exception as exc:
                st.error(f"Não foi possível importar: {exc}")
            else:
                st.success(
                    f"Plano salvo: {result['groups']} grupo(s), {result['rows']} item(ns)."
                )
                st.rerun()

    with st.expander("Importação simples/legada"):
        vocab_text = st.text_area(
            "palavra | nível | tema",
            value=DEFAULT_VOCAB_IMPORT if stats["total"] == 0 else "",
            height=100,
        )
        if st.button("Importar vocabulário simples"):
            try:
                ml_queue.import_vocabulary(vocab_text)
            except Exception as exc:
                st.error(str(exc))
            else:
                st.rerun()

    review_before_publish = st.toggle(
        "Revisar antes de publicar",
        value=bool(settings.get("review_before_publish", True)),
    )
    if review_before_publish != bool(settings.get("review_before_publish", True)):
        ml_queue.set_settings(review_before_publish=review_before_publish)
    upload_auto = bool(upload_post.upload_post_service.auto_upload)
    if review_before_publish and upload_auto:
        st.error(
            "Proteção: desative upload_post_auto_upload enquanto a revisão manual estiver ligada."
        )
    elif review_before_publish:
        st.info("Modo seguro: os vídeos aguardam sua aprovação.")

    st.write("Distribuição vertical ManyLingo: **" + ", ".join(vertical_destinations) + "**")
    for warning in distribution_warnings:
        st.warning(warning)
    if upload_post.upload_post_service.pinterest_board_id:
        st.caption(
            "Pinterest configurado: os Pins de vídeo usarão o board definido e apontarão para manylingo.com."
        )

    if stats["total"]:
        levels = ml_queue.available_levels()
        x, y, z = st.columns(3)
        batch_level = x.selectbox("Nível", levels)
        words_per_video = y.number_input("Palavras por vídeo (modo legado)", 1, 10, 5)
        batch_count = z.number_input("Quantidade de vídeos", 1, 20, 5)
        translation_language_auto = st.selectbox(
            "Idioma das traduções no fallback com IA",
            ["Spanish", "Portuguese", "French", "German", "Italian"],
        )

with st.container(border=True):
    st.subheader("1. Modo manual/teste")
    subject = st.text_input("Tema", value="English vocabulary: Home")
    words_text = st.text_area("Uma palavra por linha", value=DEFAULT_WORDS, height=100)
    translation_language = st.selectbox(
        "Idioma da tradução",
        ["Spanish", "Portuguese", "French", "German", "Italian"],
        key="manual_lang",
    )
    if st.button("Criar conteúdo manual com IA"):
        try:
            generated = generate_manylingo_items(
                words_text, translation_language=translation_language
            )
            st.session_state["manylingo_editor_text"] = items_to_editor_text(generated)
        except Exception as exc:
            st.error(str(exc))

    items_text = st.text_area(
        "Palavra | frase | tradução | termo visual",
        key="manylingo_editor_text",
        height=180,
    )

with st.container(border=True):
    st.subheader("2. Produção vertical")
    watermark = st.text_input("Marca d'água", value="manylingo.com")
    cta = st.text_area("CTA", value=DEFAULT_CTA, height=70)
    cta_duration = st.slider("Duração CTA", 0.0, 6.0, 2.5, 0.5)

    has_elevenlabs = bool(voice_service.get_elevenlabs_api_key())
    provider_options = ["Edge TTS", "ElevenLabs"] if has_elevenlabs else ["Edge TTS"]
    provider_default = 1 if has_elevenlabs else 0
    voice_provider = st.selectbox(
        "Provedor de voz",
        provider_options,
        index=provider_default,
    )
    if voice_provider == "ElevenLabs":
        elevenlabs_voices = _elevenlabs_voices()
        if elevenlabs_voices:
            voice_name = st.selectbox(
                "Voz ElevenLabs",
                elevenlabs_voices,
                format_func=_elevenlabs_voice_label,
            )
            st.caption(
                "Sincronização exata ativada: o ManyLingo usa o alinhamento temporal "
                "fornecido pelo ElevenLabs para trocar as cenas."
            )
        else:
            voice_name = ""
            st.warning(
                "A chave ElevenLabs foi encontrada, mas nenhuma voz favorita ficou disponível. "
                "Marque pelo menos uma voz como favorita no ElevenLabs e atualize esta página."
            )
            if st.button("Atualizar vozes ElevenLabs"):
                _elevenlabs_voices.clear()
                st.rerun()
    else:
        voice_name = st.text_input("Voz Edge TTS", value=_voice())
        st.caption("O Edge TTS continua usando WordBoundary para sincronização exata.")

    video_source = st.selectbox(
        "Fonte",
        ["pexels", "pixabay"],
        index=["pexels", "pixabay"].index(_source()),
    )

    left, right = st.columns(2)
    if left.button("Gerar 1 vídeo manual", use_container_width=True):
        try:
            items, narration, terms = parse_items(items_text)
            if not voice_name.strip():
                raise ValueError("Configure uma voz TTS.")
            if review_before_publish and upload_auto:
                raise ValueError(
                    "Desative upload_post_auto_upload para revisar antes de publicar."
                )
            params = build_params(
                subject=subject,
                items=items,
                narration=narration,
                search_terms=terms,
                watermark=watermark,
                cta=cta,
                cta_duration=cta_duration,
                voice_name=voice_name.strip(),
                video_source=video_source,
                aspect=VideoAspect.portrait.value,
            )
            task_id = str(uuid4())
            webui_task.submit_generation(
                task_id=task_id, params=params, capture_logs=True
            )
            st.session_state["manylingo_task_id"] = task_id
        except Exception as exc:
            st.error(str(exc))

    disabled = (
        stats["total"] == 0
        or not voice_name.strip()
        or (review_before_publish and upload_auto)
    )
    if right.button(
        f"Gerar próximo lote ({int(batch_count) if stats['total'] else 0})",
        type="primary",
        use_container_width=True,
        disabled=disabled,
    ):
        try:
            groups = ml_queue.plan_word_groups(
                level=batch_level,
                video_count=int(batch_count),
                words_per_video=int(words_per_video),
            )
            if not groups:
                raise ValueError("Não há grupos disponíveis: os próximos grupos já estão em geração.")
            progress = st.progress(0)
            for index, group in enumerate(groups, 1):
                progress.progress(
                    int((index - 1) / len(groups) * 100),
                    text=f"{index}/{len(groups)} · {group['topic']} · {', '.join(group['words'])}",
                )
                _submit_group(
                    group=group,
                    translation_language=translation_language_auto,
                    watermark=watermark,
                    cta=cta,
                    cta_duration=cta_duration,
                    voice_name=voice_name.strip(),
                    video_source=video_source,
                )
            progress.progress(100, text="Lote enviado.")
            if all(group.get("items") for group in groups):
                st.success(
                    f"{len(groups)} vídeo(s) enviados sem chamar o LLM para criar conteúdo."
                )
            else:
                st.success(
                    f"{len(groups)} vídeo(s) enviados; grupos legados usaram o fallback de IA."
                )
        except Exception as exc:
            st.error(str(exc))

with st.container(border=True):
    st.subheader("3. YouTube horizontal 16:9")
    st.caption(
        "Compila grupos fixos do mesmo tema. Ex.: 4 grupos × 5 palavras = vídeo com cerca de 20 palavras. "
        "A publicação deste formato vai somente para o YouTube tradicional."
    )
    if stats["total"]:
        h1, h2 = st.columns(2)
        horizontal_level = h1.selectbox(
            "Nível do vídeo horizontal", ml_queue.available_levels(), key="horizontal_level"
        )
        groups_per_horizontal = h2.number_input(
            "Grupos por vídeo horizontal", 2, 12, 4, key="horizontal_groups"
        )
        horizontal_disabled = (
            not voice_name.strip() or (review_before_publish and upload_auto)
        )
        if st.button(
            "Gerar próximo vídeo horizontal",
            type="primary",
            use_container_width=True,
            disabled=horizontal_disabled,
        ):
            try:
                compilation = distribution.plan_horizontal_compilation(
                    level=horizontal_level,
                    groups_per_video=int(groups_per_horizontal),
                )
                task_id = _submit_horizontal(
                    group=compilation,
                    watermark=watermark,
                    cta=cta,
                    cta_duration=cta_duration,
                    voice_name=voice_name.strip(),
                    video_source=video_source,
                )
                st.session_state["manylingo_task_id"] = task_id
                st.success(
                    f"16:9 enviado para geração: {len(compilation['words'])} palavras · "
                    f"{compilation['topic']} · grupos {', '.join(compilation['source_group_ids'])}."
                )
            except Exception as exc:
                st.error(str(exc))
    else:
        st.info("Importe o currículo pré-planejado para liberar as compilações horizontais.")

current = str(st.session_state.get("manylingo_task_id", "") or "")
if current:
    st.subheader("Status da última geração")

    @st.fragment(run_every="2s")
    def render_current():
        _task_status(current)

    render_current()

st.divider()
st.subheader("Fila ManyLingo")


@st.fragment(run_every="3s")
def render_queue():
    jobs = ml_queue.refresh_jobs()[:20]
    if not jobs:
        st.info("Nenhum vídeo automático criado ainda.")
        return
    for job in jobs:
        status = str(job.get("status") or "queued")
        is_landscape = job.get("content_format") == "landscape"
        format_label = "YouTube 16:9" if is_landscape else "Vertical"
        with st.container(border=True):
            st.markdown(
                f"**{format_label} · {job.get('group_id') or ''} · {job.get('level')} · "
                f"{job.get('topic')} · {', '.join(job.get('words') or [])}**"
            )
            if is_landscape and job.get("source_group_ids"):
                st.caption("Grupos-fonte: " + ", ".join(job["source_group_ids"]))
            st.write(f"Status: **{status}**")
            try:
                task = sm.state.get_task(str(job.get("task_id") or ""))
            except Exception:
                task = None
            if task and status in {"queued", "generating"}:
                value = max(0, min(100, int(task.get("progress", 0) or 0)))
                st.progress(value, text=f"Geração: {value}%")
            if job.get("error"):
                st.error(str(job["error"]))
            for path in job.get("video_paths") or []:
                if os.path.exists(path):
                    st.video(path)
            if status == "review":
                a, b = st.columns(2)
                if a.button(
                    "Aprovar e publicar", key=f"pub-{job['id']}", use_container_width=True
                ):
                    try:
                        distribution.publish_job_async(job["id"])
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
                if b.button("Refazer", key=f"redo-{job['id']}", use_container_width=True):
                    try:
                        models = [ManyLingoItem(**item) for item in job.get("items") or []]
                        narration = build_narration(models)
                        terms = [item.search_term or item.word for item in models]
                        params = build_params(
                            subject=str(job.get("subject") or "ManyLingo vocabulary"),
                            items=models,
                            narration=narration,
                            search_terms=terms,
                            watermark=watermark,
                            cta=cta,
                            cta_duration=cta_duration,
                            voice_name=voice_name.strip(),
                            video_source=video_source,
                            aspect=(VideoAspect.landscape.value if is_landscape else VideoAspect.portrait.value),
                        )
                        new_id = str(uuid4())
                        new_job = ml_queue.create_job(
                            task_id=new_id,
                            group={
                                "group_id": None,
                                "level": job.get("level"),
                                "topic": job.get("topic"),
                                "words": job.get("words") or [],
                                "vocabulary_ids": [],
                            },
                            items=[model.model_dump() for model in models],
                            subject=str(job.get("subject") or "ManyLingo vocabulary"),
                            narration=narration,
                        )
                        webui_task.submit_generation(task_id=new_id, params=params, capture_logs=True)
                        if is_landscape:
                            distribution.mark_horizontal_job(
                                new_job["id"],
                                {"source_group_ids": job.get("source_group_ids") or []},
                            )
                        ml_queue.set_job_status(job["id"], "failed", error="Substituído por nova geração.")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
            elif status == "failed":
                if st.button(
                    "Remover da fila",
                    key=f"remove-{job['id']}",
                    use_container_width=True,
                ):
                    ml_queue.remove_job(job["id"])
                    st.rerun()
            elif status == "publishing":
                st.info("Publicando...")
            elif status == "published":
                st.success("Publicado.")


render_queue()