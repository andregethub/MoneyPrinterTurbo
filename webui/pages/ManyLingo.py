import os
import shutil
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
from app.services import state as sm, upload_post, webui_task, voice  # noqa: E402
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
        item = ManyLingoItem(word=word, sentence=sentence, translation=translation, search_term=term or word)
        items.append(item)
        terms.append(item.search_term or item.word)
    if not items:
        raise ValueError("Adicione pelo menos uma palavra.")
    return items, build_narration(items), terms


def _voice():
    configured = str(config.ui.get("voice_name", "") or "").strip()
    return configured if configured.lower().startswith("en-") else "en-US-GuyNeural"


def _font():
    return str(config.ui.get("font_name", "MicrosoftYaHeiBold.ttc") or "MicrosoftYaHeiBold.ttc").strip()


def _source():
    source = str(config.app.get("video_source", "pexels") or "pexels").strip()
    return source if source in {"pexels", "pixabay"} else "pexels"


def _elevenlabs_voice_options() -> list[str]:
    api_key = voice.get_elevenlabs_api_key()
    return voice.get_elevenlabs_voices(api_key) if api_key else []


def _voice_label(value: str) -> str:
    value = str(value or "")
    if value.startswith("elevenlabs:"):
        parts = value.split(":", 2)
        return parts[2] if len(parts) >= 3 else value
    return value


def _safe_delete_task_files(task_id: str) -> tuple[bool, str]:
    task_id = str(task_id or "").strip()
    if not task_id:
        return False, "ID da tarefa ausente."
    try:
        task = sm.state.get_task(task_id)
    except Exception:
        task = None
    if task and task.get("state") == const.TASK_STATE_PROCESSING:
        return False, "Esse vídeo ainda está sendo gerado. Aguarde terminar antes de excluir."
    task_dir = os.path.realpath(os.path.join(root_dir, "storage", "tasks", task_id))
    tasks_root = os.path.realpath(os.path.join(root_dir, "storage", "tasks"))
    try:
        valid = os.path.commonpath([tasks_root, task_dir]) == tasks_root
    except ValueError:
        valid = False
    if not valid:
        return False, "Caminho da tarefa inválido."
    if os.path.isdir(task_dir):
        try:
            shutil.rmtree(task_dir)
        except Exception as exc:
            return False, f"Não foi possível apagar os arquivos: {exc}"
    return True, ""


def _delete_job_and_files(job: dict) -> tuple[bool, str]:
    if str(job.get("status") or "") in {"queued", "generating", "publishing"}:
        return False, "A tarefa ainda está em andamento e não pode ser excluída agora."
    ok, error = _safe_delete_task_files(str(job.get("task_id") or ""))
    if not ok:
        return False, error
    try:
        ml_queue.remove_job(job["id"])
    except Exception as exc:
        return False, f"Os arquivos foram apagados, mas não foi possível remover o item da fila: {exc}"
    return True, ""


def _ensure_group_available(group: dict) -> None:
    group_id = str(group.get("group_id") or "").strip()
    if not group_id:
        return
    for job in ml_queue.refresh_jobs():
        if str(job.get("group_id") or "") == group_id and str(job.get("status") or "") in {"queued", "generating"}:
            raise ValueError(f"O grupo {group_id} já está em geração.")


def build_params(*, subject, items, narration, search_terms, watermark, cta, cta_duration, voice_name, video_source, aspect=VideoAspect.portrait.value):
    return VideoParams(
        video_subject=subject.strip() or "ManyLingo vocabulary", video_script=narration,
        video_terms=list(search_terms), content_mode="manylingo", manylingo_items=list(items),
        manylingo_watermark=watermark.strip(), manylingo_cta=cta.strip(),
        manylingo_cta_duration=float(cta_duration), video_aspect=aspect,
        video_concat_mode=VideoConcatMode.sequential.value, match_materials_to_script=True,
        video_clip_duration=2, video_count=1, video_source=video_source, video_language="en-US",
        voice_name=voice_name, voice_volume=float(config.ui.get("voice_volume", 1.0) or 1.0),
        voice_rate=float(config.ui.get("voice_rate", 1.0) or 1.0),
        bgm_type=str(config.ui.get("bgm_type", "random") or ""),
        bgm_volume=float(config.ui.get("bgm_volume", 0.2) or 0.0), subtitle_enabled=False,
        font_name=_font(), n_threads=2,
    )


def _submit_group(*, group, translation_language, watermark, cta, cta_duration, voice_name, video_source):
    _ensure_group_available(group)
    items = [ManyLingoItem(**item) for item in group["items"]] if group.get("items") else generate_manylingo_items(group["words"], translation_language=translation_language)
    narration = build_narration(items)
    terms = [item.search_term or item.word for item in items]
    subject = f"English vocabulary: {group['topic']} ({group['level']})"
    params = build_params(subject=subject, items=items, narration=narration, search_terms=terms, watermark=watermark, cta=cta, cta_duration=cta_duration, voice_name=voice_name, video_source=video_source)
    task_id = str(uuid4())
    ml_queue.create_job(task_id=task_id, group=group, items=[item.model_dump() for item in items], subject=subject, narration=narration)
    try:
        webui_task.submit_generation(task_id=task_id, params=params, capture_logs=True)
    except Exception as exc:
        matching = next((job for job in ml_queue.list_jobs(limit=200) if job.get("task_id") == task_id), None)
        if matching:
            ml_queue.set_job_status(matching["id"], "failed", error=str(exc))
        raise
    return task_id


def _submit_horizontal(*, group, watermark, cta, cta_duration, voice_name, video_source):
    _ensure_group_available(group)
    items = [ManyLingoItem(**item) for item in group["items"]]
    narration = build_narration(items)
    terms = [item.search_term or item.word for item in items]
    subject = f"{len(items)} English Words About {group['topic']} | {group['level']} English Vocabulary"
    params = build_params(subject=subject, items=items, narration=narration, search_terms=terms, watermark=watermark, cta=cta, cta_duration=cta_duration, voice_name=voice_name, video_source=video_source, aspect=VideoAspect.landscape.value)
    task_id = str(uuid4())
    job = ml_queue.create_job(task_id=task_id, group={**group, "vocabulary_ids": []}, items=[item.model_dump() for item in items], subject=subject, narration=narration)
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
    elif state != const.TASK_STATE_COMPLETE:
        st.progress(progress, text=f"Gerando vídeo: {progress}%")
    else:
        st.success("Vídeo concluído. Veja e revise na galeria abaixo.")


st.set_page_config(page_title="ManyLingo", page_icon="🌎", layout="wide")

# Stable container keys become st-key-* CSS classes in Streamlit. This lets the
# persistent queue status drive the card border without changing the queue data model.
st.markdown("""
<style>
[class*="st-key-ml-card-review-"] { border: 3px solid #ef4444 !important; border-radius: 12px !important; }
[class*="st-key-ml-card-published-"] { border: 3px solid #22c55e !important; border-radius: 12px !important; }
[class*="st-key-ml-card-publishing-"] { border: 3px solid #f59e0b !important; border-radius: 12px !important; }
[class*="st-key-ml-card-working-"] { border: 3px solid #f59e0b !important; border-radius: 12px !important; }
[class*="st-key-ml-card-failed-"] { border: 3px solid #6b7280 !important; border-radius: 12px !important; }
.ml-status { font-size: .82rem; font-weight: 700; margin: -.15rem 0 .45rem 0; }
.ml-review { color: #ef4444; }
.ml-published { color: #22c55e; }
.ml-publishing, .ml-working { color: #f59e0b; }
.ml-failed { color: #6b7280; }
</style>
""", unsafe_allow_html=True)

if "manylingo_editor_text" not in st.session_state:
    st.session_state["manylingo_editor_text"] = DEFAULT_ITEMS
ml_queue.refresh_jobs()
settings = ml_queue.get_settings()
stats = ml_queue.vocabulary_stats()
all_jobs = ml_queue.list_jobs(limit=200)
failed_jobs = [job for job in all_jobs if job.get("status") == "failed"]
vertical_destinations, distribution_warnings = distribution.vertical_platforms()
upload_auto = bool(upload_post.upload_post_service.auto_upload)

st.title("ManyLingo")
st.caption("Criação automática de vídeos educativos · conteúdo → voz → cenas → revisão → publicação")
content_col, video_col, audio_col, automation_col = st.columns(4, gap="small")
with content_col:
    with st.container(border=True):
        st.markdown("#### Conteúdo")
        subject = st.text_area("Tema do vídeo", value="English vocabulary: Home", height=84)
        translation_language = st.selectbox("Idioma da tradução", ["Spanish", "Portuguese", "French", "German", "Italian"], key="manual_lang")
        words_text = st.text_area("Palavras", value=DEFAULT_WORDS, height=104, help="Uma palavra ou expressão por linha.")
        if st.button("✨ Criar conteúdo com IA", use_container_width=True):
            try:
                generated = generate_manylingo_items(words_text, translation_language=translation_language)
                st.session_state["manylingo_editor_text"] = items_to_editor_text(generated)
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
with video_col:
    with st.container(border=True):
        st.markdown("#### Vídeo")
        video_source = st.selectbox("Fonte dos vídeos", ["pexels", "pixabay"], index=["pexels", "pixabay"].index(_source()))
        st.selectbox("Formato principal", ["Vertical 9:16"], disabled=True)
        watermark = st.text_input("Marca d'água", value="manylingo.com")
        with st.expander("CTA e aparência"):
            cta = st.text_area("CTA", value=DEFAULT_CTA, height=72)
            cta_duration = st.slider("Duração CTA", 0.0, 6.0, 2.5, 0.5)
with audio_col:
    with st.container(border=True):
        st.markdown("#### Áudio")
        elevenlabs_voices = _elevenlabs_voice_options()
        providers = ["Edge TTS"] + (["ElevenLabs"] if elevenlabs_voices else [])
        default_provider = "ElevenLabs" if elevenlabs_voices else "Edge TTS"
        voice_provider = st.selectbox("Provedor de voz", providers, index=providers.index(default_provider))
        if voice_provider == "ElevenLabs":
            voice_name = st.selectbox("Voz", elevenlabs_voices, format_func=_voice_label)
            st.caption("ElevenLabs · ritmo didático · timestamps reais")
        else:
            voice_name = st.text_input("Voz", value=_voice())
            st.caption("Edge TTS · gratuito · timestamps reais")
        st.caption("A troca de cenas segue os timestamps da narração.")
with automation_col:
    with st.container(border=True):
        st.markdown("#### Automação")
        review_before_publish = st.toggle("Revisar antes de publicar", value=bool(settings.get("review_before_publish", True)))
        if review_before_publish != bool(settings.get("review_before_publish", True)):
            ml_queue.set_settings(review_before_publish=review_before_publish)
        if review_before_publish and upload_auto:
            st.error("Desative o upload automático para usar revisão manual.")
        elif review_before_publish:
            st.success("Revisão manual ativada")
        st.metric("Palavras", stats["total"])
        a, b = st.columns(2)
        a.metric("Não usadas", stats["unused"])
        b.metric("Fila", len(all_jobs))

st.space("small")
with st.container(border=True):
    editor_left, editor_right = st.columns([2.2, 1], gap="medium", vertical_alignment="top")
    with editor_left:
        st.markdown("#### Conteúdo do vídeo")
        st.caption("Edite somente se quiser. Formato: palavra | frase | tradução | termo visual")
        items_text = st.text_area("Conteúdo estruturado", key="manylingo_editor_text", height=150, label_visibility="collapsed")
    with editor_right:
        st.markdown("#### Gerar")
        st.caption("Use o manual para testes rápidos ou o lote para produção automática.")
        manual_disabled = not str(voice_name or "").strip() or (review_before_publish and upload_auto)
        if st.button("▶ Gerar 1 vídeo manual", type="primary", use_container_width=True, disabled=manual_disabled):
            try:
                items, narration, terms = parse_items(items_text)
                params = build_params(subject=subject, items=items, narration=narration, search_terms=terms, watermark=watermark, cta=cta, cta_duration=cta_duration, voice_name=voice_name.strip(), video_source=video_source)
                task_id = str(uuid4())
                webui_task.submit_generation(task_id=task_id, params=params, capture_logs=True)
                st.session_state["manylingo_task_id"] = task_id
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
        if stats["total"]:
            levels = ml_queue.available_levels()
            batch_level = st.selectbox("Nível do lote", levels, key="batch_level")
            batch_count = st.number_input("Quantidade", 1, 20, 5, key="batch_count")
            words_per_video = st.number_input("Palavras por vídeo", 1, 10, 5, key="batch_words")
            translation_language_auto = st.selectbox("Tradução do fallback IA", ["Spanish", "Portuguese", "French", "German", "Italian"], key="batch_lang")
            batch_disabled = not str(voice_name or "").strip() or (review_before_publish and upload_auto)
            if st.button(f"Gerar lote ({int(batch_count)})", use_container_width=True, disabled=batch_disabled):
                try:
                    groups = ml_queue.plan_word_groups(level=batch_level, video_count=int(batch_count), words_per_video=int(words_per_video))
                    if not groups:
                        raise ValueError("Não há grupos disponíveis para gerar agora.")
                    progress = st.progress(0)
                    for index, group in enumerate(groups, 1):
                        progress.progress(int((index - 1) / len(groups) * 100), text=f"{index}/{len(groups)} · {group['topic']}")
                        _submit_group(group=group, translation_language=translation_language_auto, watermark=watermark, cta=cta, cta_duration=cta_duration, voice_name=voice_name.strip(), video_source=video_source)
                    progress.progress(100, text="Lote enviado.")
                    st.success(f"{len(groups)} vídeo(s) enviados para geração.")
                except Exception as exc:
                    st.error(str(exc))
        else:
            st.info("Importe um currículo em Avançado para liberar lotes.")

with st.expander("⚙️ Avançado · currículo, distribuição e YouTube 16:9"):
    adv1, adv2 = st.columns(2, gap="large")
    with adv1:
        st.markdown("##### Currículo")
        plan_text = st.text_area("Plano pré-planejado", value=DEFAULT_PLAN_IMPORT if stats["total"] == 0 else "", height=170, help="video_id | nível | tema | ordem | palavra | frase | tradução | termo visual")
        if st.button("Importar plano fixo", use_container_width=True):
            try:
                result = ml_queue.import_preplanned_curriculum(plan_text)
                st.success(f"Plano salvo: {result['groups']} grupo(s), {result['rows']} item(ns).")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
        with st.expander("Importação simples/legada"):
            vocab_text = st.text_area("palavra | nível | tema", value=DEFAULT_VOCAB_IMPORT if stats["total"] == 0 else "", height=90)
            if st.button("Importar vocabulário simples"):
                try:
                    ml_queue.import_vocabulary(vocab_text)
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
    with adv2:
        st.markdown("##### Distribuição")
        st.write("Vertical: **" + ", ".join(vertical_destinations) + "**")
        for warning in distribution_warnings:
            st.warning(warning)
        if upload_post.upload_post_service.pinterest_board_id:
            st.caption("Pinterest configurado para apontar para manylingo.com.")
        st.divider()
        st.markdown("##### YouTube horizontal 16:9")
        if stats["total"]:
            horizontal_level = st.selectbox("Nível", ml_queue.available_levels(), key="horizontal_level")
            groups_per_horizontal = st.number_input("Grupos por vídeo", 2, 12, 4, key="horizontal_groups")
            if st.button("Gerar vídeo horizontal", use_container_width=True, disabled=manual_disabled):
                try:
                    compilation = distribution.plan_horizontal_compilation(level=horizontal_level, groups_per_video=int(groups_per_horizontal))
                    task_id = _submit_horizontal(group=compilation, watermark=watermark, cta=cta, cta_duration=cta_duration, voice_name=voice_name.strip(), video_source=video_source)
                    st.session_state["manylingo_task_id"] = task_id
                    st.success(f"16:9 enviado: {len(compilation['words'])} palavras.")
                except Exception as exc:
                    st.error(str(exc))
        else:
            st.caption("Importe o currículo para liberar esta opção.")
    if failed_jobs:
        st.divider()
        st.warning(f"Há {len(failed_jobs)} tarefa(s) interrompida(s) ou com falha.")
        if st.button(f"Limpar tarefas interrompidas ({len(failed_jobs)})"):
            for failed_job in failed_jobs:
                ml_queue.remove_job(failed_job["id"])
            st.rerun()

current = str(st.session_state.get("manylingo_task_id", "") or "")
if current:
    @st.fragment(run_every="2s")
    def render_current():
        _task_status(current)
    render_current()

st.markdown("### Fila e revisão")
st.caption("🔴 revisar · 🟠 gerando/publicando · 🟢 publicado · 4 vídeos por linha")


def _card_visual_status(status: str) -> tuple[str, str, str]:
    if status == "review":
        return "review", "● Aguardando revisão", "ml-review"
    if status == "published":
        return "published", "● Publicado", "ml-published"
    if status == "publishing":
        return "publishing", "● Publicando", "ml-publishing"
    if status == "failed":
        return "failed", "● Falhou", "ml-failed"
    return "working", "● Gerando", "ml-working"


def _render_job_card(job: dict) -> None:
    status = str(job.get("status") or "queued")
    visual_status, status_label, status_class = _card_visual_status(status)
    is_landscape = job.get("content_format") == "landscape"
    format_label = "YouTube 16:9" if is_landscape else "Vertical 9:16"
    words = ", ".join(job.get("words") or [])
    paths = [path for path in job.get("video_paths") or [] if os.path.exists(path)]
    safe_id = "".join(ch if ch.isalnum() else "-" for ch in str(job.get("id") or "job"))

    with st.container(border=True, key=f"ml-card-{visual_status}-{safe_id}"):
        st.markdown(f'<div class="ml-status {status_class}">{status_label}</div>', unsafe_allow_html=True)
        if paths:
            st.video(paths[0])
        else:
            st.caption("Prévia disponível quando concluir.")
        st.markdown(f"**{job.get('topic') or 'ManyLingo'}**")
        st.caption(f"{format_label} · {job.get('level') or '—'}")
        if words:
            short_words = words if len(words) <= 72 else words[:69] + "..."
            st.caption(short_words)
        try:
            task = sm.state.get_task(str(job.get("task_id") or ""))
        except Exception:
            task = None
        if task and status in {"queued", "generating"}:
            value = max(0, min(100, int(task.get("progress", 0) or 0)))
            st.progress(value, text=f"{value}%")
        if job.get("error"):
            with st.expander("Ver erro"):
                st.error(str(job["error"]))
        if status == "review":
            if st.button("✓ Aprovar", key=f"pub-{job['id']}", use_container_width=True):
                try:
                    distribution.publish_job_async(job["id"])
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
            if st.button("↻ Refazer", key=f"redo-{job['id']}", use_container_width=True):
                try:
                    models = [ManyLingoItem(**item) for item in job.get("items") or []]
                    narration = build_narration(models)
                    terms = [item.search_term or item.word for item in models]
                    params = build_params(subject=str(job.get("subject") or "ManyLingo vocabulary"), items=models, narration=narration, search_terms=terms, watermark=watermark, cta=cta, cta_duration=cta_duration, voice_name=voice_name.strip(), video_source=video_source, aspect=VideoAspect.landscape.value if is_landscape else VideoAspect.portrait.value)
                    new_id = str(uuid4())
                    new_job = ml_queue.create_job(task_id=new_id, group={"group_id": None, "level": job.get("level"), "topic": job.get("topic"), "words": job.get("words") or [], "vocabulary_ids": []}, items=[model.model_dump() for model in models], subject=str(job.get("subject") or "ManyLingo vocabulary"), narration=narration)
                    webui_task.submit_generation(task_id=new_id, params=params, capture_logs=True)
                    if is_landscape:
                        distribution.mark_horizontal_job(new_job["id"], {"source_group_ids": job.get("source_group_ids") or []})
                    ml_queue.set_job_status(job["id"], "failed", error="Substituído por nova geração.")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
            if st.button("🗑 Excluir", key=f"delete-{job['id']}", use_container_width=True):
                ok, error = _delete_job_and_files(job)
                st.rerun() if ok else st.error(error)
        elif status == "failed":
            if st.button("Remover", key=f"remove-{job['id']}", use_container_width=True):
                ml_queue.remove_job(job["id"])
                st.rerun()
            if st.button("Excluir arquivos", key=f"delete-failed-{job['id']}", use_container_width=True):
                ok, error = _delete_job_and_files(job)
                st.rerun() if ok else st.error(error)
        elif status == "published":
            if st.button("Excluir cópia local", key=f"delete-published-{job['id']}", use_container_width=True):
                ok, error = _delete_job_and_files(job)
                st.rerun() if ok else st.error(error)
        elif status == "publishing":
            st.info("Enviando para as redes...")
        else:
            st.caption("Aguardando conclusão")


@st.fragment(run_every="3s")
def render_queue():
    jobs = ml_queue.refresh_jobs()[:20]
    if not jobs:
        st.info("Nenhum vídeo na fila.")
        return
    for start in range(0, len(jobs), 4):
        row_jobs = jobs[start:start + 4]
        columns = st.columns(4, gap="small", vertical_alignment="top")
        for column, job in zip(columns, row_jobs):
            with column:
                _render_job_card(job)


render_queue()
