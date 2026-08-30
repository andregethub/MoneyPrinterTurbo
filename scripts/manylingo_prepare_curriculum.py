"""Prepare the local ManyLingo curriculum from user-supplied CEFR PDFs.

Run locally with the two user-supplied PDFs. The PDFs and extracted word list are
not committed. `pdftotext` (Poppler) avoids adding a Python PDF dependency.
The optional enrichment step runs once and checkpoints every completed group;
daily video generation then reuses the saved content without LLM calls.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from app.services import manylingo_queue
from app.services.manylingo import generate_manylingo_items
from app.services.manylingo_content_safety import safe_visual_term
from app.services.manylingo_curriculum import build_fixed_groups, curriculum_to_import_text, parse_cefr_text


def extract_pdf_text(path: str | Path) -> str:
    source = Path(path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    with tempfile.NamedTemporaryFile(suffix=".txt") as temp:
        try:
            subprocess.run(["pdftotext", "-raw", str(source), temp.name], check=True, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise RuntimeError("pdftotext não foi encontrado. Instale o Poppler ou exporte o PDF para texto.") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(exc.stderr or "Falha ao extrair texto do PDF.") from exc
        return Path(temp.name).read_text(encoding="utf-8", errors="replace")


def load_entries(oxford_3000: str, oxford_5000: str) -> list[dict]:
    entries = parse_cefr_text(extract_pdf_text(oxford_3000), source="Oxford 3000 user PDF")
    entries += parse_cefr_text(extract_pdf_text(oxford_5000), source="Oxford 5000 user PDF")
    if not entries:
        raise RuntimeError("Nenhuma entrada CEFR foi encontrada nos PDFs fornecidos.")
    return entries


def enrich_groups(groups: list[dict], checkpoint: Path, translation_language: str) -> list[dict]:
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        by_id = {group["video_id"]: group for group in saved}
        groups = [by_id.get(group["video_id"], group) for group in groups]
    for index, group in enumerate(groups, start=1):
        if all(item.get("sentence") and item.get("translation") for item in group["items"]):
            continue
        words = [item["word"] for item in group["items"]]
        generated = generate_manylingo_items(words, translation_language=translation_language)
        generated_by_word = {item.word.casefold(): item for item in generated}
        for item in group["items"]:
            generated_item = generated_by_word[item["word"].casefold()]
            item["sentence"] = generated_item.sentence
            item["translation"] = generated_item.translation
            item["search_term"] = safe_visual_term(item["word"], generated_item.search_term)
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text(json.dumps(groups, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[{index}/{len(groups)}] pronto: {group['video_id']} · {group['topic']}")
    return groups


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare o currículo fixo do ManyLingo.")
    parser.add_argument("--oxford-3000", required=True)
    parser.add_argument("--oxford-5000", required=True)
    parser.add_argument("--words-per-video", type=int, default=5)
    parser.add_argument("--translation-language", default="Spanish")
    parser.add_argument("--enrich", action="store_true", help="Gera frase/tradução/termo visual uma única vez usando o LLM configurado.")
    parser.add_argument("--import-store", action="store_true", help="Importa o plano pronto no storage local do ManyLingo.")
    parser.add_argument("--output", default="storage/manylingo/curriculum.json")
    args = parser.parse_args()

    entries = load_entries(args.oxford_3000, args.oxford_5000)
    groups = build_fixed_groups(entries, words_per_video=args.words_per_video)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    print(f"Entradas CEFR lidas: {len(entries)}")
    print(f"Grupos planejados: {len(groups)}")
    print("Headwords repetidos são ensinados uma vez no primeiro nível CEFR oficial em que aparecem.")
    print("Níveis vêm da fonte; temas e agrupamentos são organização ManyLingo.")

    if args.enrich:
        groups = enrich_groups(groups, output, args.translation_language)
    else:
        output.write_text(json.dumps(groups, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.import_store:
        if not args.enrich:
            raise RuntimeError("Use --enrich antes de --import-store para preencher frases e traduções.")
        result = manylingo_queue.import_preplanned_curriculum(curriculum_to_import_text(groups))
        print(f"Importado: {result['groups']} grupos / {result['rows']} itens.")
    print(f"Currículo salvo em: {output}")


if __name__ == "__main__":
    main()
