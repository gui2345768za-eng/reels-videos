"""
Publicador em nuvem do Reels Studio — roda no GitHub Actions.

Lê agenda/agenda.json (enviada pelo app), publica os Reels cujo horário já
chegou e grava o resultado em agenda/resultados.json.

Segredos necessários (Settings > Secrets and variables > Actions):
  IG_ID     -> Instagram User ID
  IG_TOKEN  -> Instagram Access Token
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API = "https://graph.instagram.com/v26.0"
AGENDA = Path("agenda/agenda.json")
RESULTADOS = Path("agenda/resultados.json")
MAX_POR_EXECUCAO = 3
TEMPO_MAX_PUBLICANDO = timedelta(minutes=30)
ESPERA_PROCESSAMENTO = 600  # segundos

IG_ID = os.environ.get("IG_ID", "").strip()
TOKEN = os.environ.get("IG_TOKEN", "").strip()


def agora():
    return datetime.now(timezone.utc)


def ler(caminho, padrao):
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return padrao


def gravar(resultados):
    RESULTADOS.parent.mkdir(parents=True, exist_ok=True)
    RESULTADOS.write_text(
        json.dumps(resultados, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def git(*args, check=True):
    return subprocess.run(["git", *args], check=check,
                          capture_output=True, text=True)


def commitar(mensagem):
    """Grava resultados.json no repositório (com novas tentativas)."""
    git("config", "user.name", "reels-studio-bot")
    git("config", "user.email", "reels-studio-bot@users.noreply.github.com")
    git("add", str(RESULTADOS))
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return
    git("commit", "-m", mensagem)
    for _ in range(4):
        git("pull", "--rebase", "--autostash", check=False)
        if git("push", check=False).returncode == 0:
            return
        time.sleep(3)
    raise RuntimeError("Não foi possível enviar o resultado ao repositório.")


def erro_api(dados):
    if isinstance(dados, dict):
        err = dados.get("error")
        if isinstance(err, dict):
            return err.get("message") or json.dumps(err, ensure_ascii=False)
    return json.dumps(dados, ensure_ascii=False)


def publicar_no_instagram(item):
    """Cria o contêiner, espera processar e publica. Retorna o ID do post."""
    corpo = {
        "media_type": "REELS",
        "video_url": item["url"],
        "caption": item.get("legenda", ""),
        "share_to_feed": "true",
        "access_token": TOKEN,
    }
    if str(item.get("capa_url", "")).startswith("https://"):
        corpo["cover_url"] = item["capa_url"]
    elif item.get("capa_segundo"):
        segundos = float(str(item["capa_segundo"]).replace(",", "."))
        corpo["thumb_offset"] = str(int(segundos * 1000))

    r = requests.post(f"{API}/{IG_ID}/media", data=corpo, timeout=60)
    dados = r.json()
    if not r.ok or "id" not in dados:
        raise RuntimeError(f"Erro ao criar contêiner: {erro_api(dados)}")
    container_id = dados["id"]

    limite = time.time() + ESPERA_PROCESSAMENTO
    while time.time() < limite:
        r = requests.get(
            f"{API}/{container_id}",
            params={"fields": "status_code", "access_token": TOKEN},
            timeout=30,
        )
        estado = r.json()
        if not r.ok:
            raise RuntimeError(f"Erro ao consultar status: {erro_api(estado)}")
        codigo = estado.get("status_code")
        if codigo == "FINISHED":
            break
        if codigo == "ERROR":
            raise RuntimeError(f"O Instagram não processou o vídeo: {estado}")
        time.sleep(5)
    else:
        raise RuntimeError("Tempo esgotado aguardando o processamento do vídeo.")

    r = requests.post(
        f"{API}/{IG_ID}/media_publish",
        data={"creation_id": container_id, "access_token": TOKEN},
        timeout=60,
    )
    resultado = r.json()
    if not r.ok or "id" not in resultado:
        raise RuntimeError(f"Erro ao publicar: {erro_api(resultado)}")
    return resultado["id"]


def main():
    if not IG_ID or not TOKEN:
        print("Configure os segredos IG_ID e IG_TOKEN no repositório.")
        return 1

    agenda = ler(AGENDA, [])
    resultados = ler(RESULTADOS, {})
    if not isinstance(agenda, list) or not isinstance(resultados, dict):
        print("Arquivos de agenda inválidos.")
        return 1

    # Itens "Publicando" há muito tempo: marca como erro. Não tenta de novo
    # automaticamente, para nunca publicar o mesmo Reel duas vezes.
    mudou = False
    for r in resultados.values():
        if r.get("status") != "Publicando":
            continue
        try:
            inicio = datetime.fromisoformat(r["quando"])
        except (KeyError, ValueError):
            inicio = None
        if inicio is None or agora() - inicio > TEMPO_MAX_PUBLICANDO:
            r["status"] = "Erro"
            r["erro"] = ("Publicação interrompida. Confira no Instagram se o "
                         "Reel foi postado antes de agendar de novo.")
            mudou = True
    if mudou:
        gravar(resultados)
        commitar("Atualiza itens interrompidos")

    vencidos = []
    for item in agenda:
        id_ = item.get("id")
        if not id_ or id_ in resultados:
            continue
        try:
            quando = datetime.fromisoformat(item["data_utc"])
        except (KeyError, ValueError):
            continue
        if quando <= agora() and str(item.get("url", "")).startswith("https://"):
            vencidos.append((quando, item))
    vencidos.sort(key=lambda t: t[0])

    if not vencidos:
        print("Nada para publicar agora.")
        return 0

    for _, item in vencidos[:MAX_POR_EXECUCAO]:
        id_ = item["id"]
        print(f"Publicando {id_}…")

        # Registra ANTES de publicar: se algo falhar depois, o próximo ciclo
        # não repete o post. Se este commit falhar, nada é publicado.
        resultados[id_] = {"status": "Publicando", "quando": agora().isoformat()}
        gravar(resultados)
        commitar(f"Publicando {id_}")

        try:
            post_id = publicar_no_instagram(item)
            resultados[id_] = {"status": "Publicado", "publicacao_id": post_id,
                               "quando": agora().isoformat()}
            print(f"Publicado: {post_id}")
        except Exception as exc:
            resultados[id_] = {"status": "Erro", "erro": str(exc),
                               "quando": agora().isoformat()}
            print(f"Erro: {exc}")

        gravar(resultados)
        try:
            commitar(f"Resultado {id_}")
        except Exception as exc:
            print(f"Aviso: resultado não enviado ao repositório: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
