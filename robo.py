"""
Robô da newsletter.
Lê os portais de fontes.txt, filtra pelos temas de temas.txt,
pede um resumo curto ao Gemini (plano gratuito) e monta a página em site/index.html.
Se o Gemini não responder (limite grátis, chave errada), usa o resumo do próprio RSS.
"""

import hashlib
import html
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

import feedparser
import requests

# ---------------- CONFIGURAÇÃO ----------------
NOME_NEWSLETTER = "Radar de Ingredientes"
SUBTITULO = "Açúcar, trigo, arroz, óleos, cacau, lácteos e coco — atualizado 3x ao dia"
DIAS_NA_PAGINA = 7          # notícias dos últimos N dias aparecem na página
DIAS_NO_HISTORICO = 30      # quanto tempo o robô lembra das notícias
MAX_IA_POR_RODADA = 90      # máximo de matérias enviadas ao Gemini por rodada
LOTE_IA = 15                # matérias por chamada ao Gemini
PAUSA_ENTRE_CHAMADAS = 13   # segundos entre chamadas (respeita o limite por minuto do plano grátis)
MODELO = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")
# ----------------------------------------------

BASE = Path(__file__).parent
DADOS = BASE / "data"
SITE = BASE / "site"
ARQ_NOTICIAS = DADOS / "noticias.json"
ARQ_FEEDS = DADOS / "feeds_descobertos.json"
FUSO = timezone(timedelta(hours=-3))  # horário de Brasília
AGORA = datetime.now(timezone.utc)
NAVEGADOR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


# ---------- utilidades ----------
def normalizar(texto):
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto.lower()).strip()


def limpar_html(texto):
    texto = re.sub(r"<[^>]+>", " ", texto or "")
    return re.sub(r"\s+", " ", html.unescape(texto)).strip()


def encurtar(texto, limite):
    if len(texto) <= limite:
        return texto
    corte = texto[:limite].rsplit(" ", 1)[0]
    return corte.rstrip(".,;:") + "…"


def ler_json(caminho, padrao):
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return padrao


def salvar_json(caminho, dados):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- leitura dos arquivos de configuração ----------
def ler_fontes():
    fontes = []
    for linha in (BASE / "fontes.txt").read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "|" not in linha:
            continue
        nome, url = [p.strip() for p in linha.split("|", 1)]
        if not url.startswith("http"):
            url = "https://" + url
        fontes.append({"nome": nome, "url": url})
    return fontes


def ler_temas():
    temas, atual = [], None
    for linha in (BASE / "temas.txt").read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#"):
            continue
        if linha.startswith("[") and linha.endswith("]"):
            atual = {"nome": linha[1:-1].strip(), "foco": "", "palavras": [], "nota": False}
            temas.append(atual)
        elif atual and ":" in linha:
            chave, valor = [p.strip() for p in linha.split(":", 1)]
            chave = normalizar(chave)
            if chave == "foco":
                atual["foco"] = valor
            elif chave == "palavras":
                atual["palavras"] = [normalizar(p) for p in valor.split(",") if p.strip()]
            elif chave == "tipo":
                atual["nota"] = normalizar(valor) == "nota"
    return temas


# ---------- descoberta do RSS ----------
def baixar(url, timeout=25):
    r = requests.get(url, headers=NAVEGADOR, timeout=timeout)
    r.raise_for_status()
    return r


def eh_feed_valido(url):
    try:
        feed = feedparser.parse(baixar(url, timeout=10).content)
        return len(feed.entries) > 0
    except Exception:
        return False


def descobrir_feed(site):
    candidatos = []
    try:
        pagina = baixar(site).text
        for tag in re.findall(r"<link[^>]+>", pagina, flags=re.I):
            if re.search(r"application/(rss|atom)\+xml", tag, re.I):
                href = re.search(r'href=["\']([^"\']+)', tag, re.I)
                if href:
                    candidatos.append(urljoin(site, html.unescape(href.group(1))))
    except Exception:
        pass
    raiz = f"{urlparse(site).scheme}://{urlparse(site).netloc}"
    base = site if site.endswith("/") else site + "/"
    for caminho in ("feed/", "rss/", "rss.xml", "feed.xml", "rss", "atom.xml"):
        candidatos += [urljoin(base, caminho), urljoin(raiz + "/", caminho)]
    vistos = set()
    for url in candidatos:
        if url in vistos:
            continue
        vistos.add(url)
        if eh_feed_valido(url):
            return url
    return None


def feed_google_noticias(site):
    dominio = urlparse(site).netloc.replace("www.", "")
    busca = quote_plus(f"site:{dominio} when:7d")
    return f"https://news.google.com/rss/search?q={busca}&hl=pt-BR&gl=BR&ceid=BR:pt-419"


# ---------- coleta ----------
def coletar(fontes, cache_feeds):
    materias, status = [], []
    for fonte in fontes:
        site = fonte["url"]
        guardado = cache_feeds.get(site) or ""
        metodo = "RSS do portal"
        feed_url = None
        if guardado.startswith("SEM_RSS|"):
            # portal sem RSS: só tenta descobrir de novo depois de 7 dias
            checado = datetime.fromisoformat(guardado.split("|", 1)[1])
            if AGORA - checado < timedelta(days=7):
                feed_url, metodo = feed_google_noticias(site), "Google Notícias"
        elif guardado and eh_feed_valido(guardado):
            feed_url = guardado
        if not feed_url:
            feed_url = descobrir_feed(site)
            if not feed_url:
                feed_url, metodo = feed_google_noticias(site), "Google Notícias"
                cache_feeds[site] = "SEM_RSS|" + AGORA.isoformat()
            else:
                cache_feeds[site] = feed_url

        try:
            feed = feedparser.parse(baixar(feed_url).content)
            entradas = feed.entries[:40]
        except Exception as erro:
            entradas = []
            metodo += f" (erro: {type(erro).__name__})"

        for e in entradas:
            link = e.get("link", "").strip()
            titulo = limpar_html(e.get("title", ""))
            if not link or not titulo:
                continue
            if metodo.startswith("Google"):
                titulo = re.sub(r"\s+-\s+[^-]+$", "", titulo)  # tira " - Nome do site" do fim
            data = e.get("published_parsed") or e.get("updated_parsed")
            data = datetime(*data[:6], tzinfo=timezone.utc) if data else AGORA
            materias.append({
                "id": hashlib.sha1(link.encode()).hexdigest()[:16],
                "titulo": titulo,
                "link": link,
                "fonte": fonte["nome"],
                "data": data.isoformat(),
                "trecho": encurtar(limpar_html(e.get("summary", "")), 600),
            })
        status.append({"fonte": fonte["nome"], "metodo": metodo, "materias": len(entradas)})
        print(f"{fonte['nome']}: {len(entradas)} matérias via {metodo}")
    return materias, status


# ---------- filtro por tema ----------
def temas_por_palavra(materia, temas):
    texto = " " + normalizar(materia["titulo"] + " " + materia["trecho"]) + " "
    pontos = {}
    for tema in temas:
        n = sum(1 for palavra in tema["palavras"]
                if re.search(r"(?<![a-z0-9])" + re.escape(palavra) + r"(?![a-z0-9])", texto))
        if n:
            pontos[tema["nome"]] = n
    # o tema com mais palavras encontradas vem primeiro
    return sorted(pontos, key=lambda nome: -pontos[nome])


# ---------- resumo com Gemini ----------
def resumir_lote(lote, temas, chave):
    nomes = [t["nome"] for t in temas]
    descricao = "\n".join(f"- {t['nome']}: {t['foco']}" for t in temas)
    itens = "\n\n".join(
        f"[{i}] Fonte: {m['fonte']}\nTítulo: {m['titulo']}\nTrecho: {m['trecho'] or '(sem trecho)'}"
        for i, m in enumerate(lote)
    )
    instrucao = f"""Você ajuda uma empresa que vende ingredientes para a indústria de alimentos no Brasil.
Temas de interesse:
{descricao}

Para cada notícia abaixo:
1. Escolha UM tema da lista ({", ".join(nomes)}) ou "fora" se a notícia não tiver relação real com o foco dos temas.
2. Escreva um resumo em português, com 2 frases curtas, só com fatos que estão no título e no trecho. Não invente números, não opine e não copie frases inteiras: reescreva com suas palavras.
3. Dê uma nota de relevância de 1 a 5 para quem compra e vende esses ingredientes.

Responda só com JSON neste formato:
[{{"i": 0, "tema": "...", "resumo": "...", "relevancia": 3}}]

Notícias:
{itens}"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODELO}:generateContent"
    corpo = {
        "contents": [{"parts": [{"text": instrucao}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }
    r = requests.post(url, headers={"x-goog-api-key": chave}, json=corpo, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini respondeu {r.status_code}: {r.text[:200]}")
    texto = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    return {int(x["i"]): x for x in json.loads(texto) if "i" in x}


def aplicar_ia(pendentes, temas):
    chave = os.environ.get("GEMINI_API_KEY", "").strip()
    if not chave:
        print("Sem chave do Gemini: usando o resumo do RSS.")
        return
    nomes = {normalizar(t["nome"]): t["nome"] for t in temas}
    pendentes = pendentes[:MAX_IA_POR_RODADA]
    for inicio in range(0, len(pendentes), LOTE_IA):
        lote = pendentes[inicio:inicio + LOTE_IA]
        try:
            respostas = resumir_lote(lote, temas, chave)
        except Exception as erro:
            print(f"Gemini indisponível nesta rodada ({erro}). Usando o resumo do RSS.")
            return
        for i, m in enumerate(lote):
            resp = respostas.get(i)
            if not resp:
                continue
            tema = normalizar(str(resp.get("tema", "")))
            if tema == "fora":
                m["tema"] = None
            elif tema in nomes:
                m["tema"] = nomes[tema]
            if resp.get("resumo"):
                m["resumo"] = str(resp["resumo"]).strip()
            m["relevancia"] = int(resp.get("relevancia") or 3)
            m["ia"] = True
        if inicio + LOTE_IA < len(pendentes):
            time.sleep(PAUSA_ENTRE_CHAMADAS)


# ---------- página ----------
def montar_pagina(noticias, temas, status):
    corte = AGORA - timedelta(days=DIAS_NA_PAGINA)
    visiveis = [n for n in noticias if n.get("tema") and datetime.fromisoformat(n["data"]) >= corte]
    visiveis.sort(key=lambda n: n["data"], reverse=True)

    def cartao(n):
        data = datetime.fromisoformat(n["data"]).astimezone(FUSO)
        resumo = n.get("resumo") or n["trecho"] or ""
        aviso = "" if n.get("ia") else '<span class="aviso">resumo do portal</span>'
        destaque = " destaque" if n.get("relevancia", 0) >= 4 else ""
        return f"""<article class="cartao{destaque}" data-tema="{html.escape(n['tema'])}">
  <div class="meta"><span class="tema">{html.escape(n['tema'])}</span>
  <span>{html.escape(n['fonte'])} · {data:%d/%m %H:%M}</span>{aviso}</div>
  <h2><a href="{html.escape(n['link'])}" target="_blank" rel="noopener">{html.escape(n['titulo'])}</a></h2>
  <p>{html.escape(encurtar(resumo, 420))}</p>
</article>"""

    botoes = "".join(
        f'<button data-filtro="{html.escape(t["nome"])}">{html.escape(t["nome"])}'
        f'{" (nota)" if t["nota"] else ""}</button>' for t in temas
    )
    linhas_status = "".join(
        f"<li>{html.escape(s['fonte'])}: {s['materias']} matérias via {html.escape(s['metodo'])}</li>"
        for s in status
    )
    cartoes = "\n".join(cartao(n) for n in visiveis) or "<p>Nenhuma notícia dos temas nos últimos dias.</p>"
    atualizado = AGORA.astimezone(FUSO).strftime("%d/%m/%Y às %H:%M")

    pagina = f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(NOME_NEWSLETTER)}</title>
<style>
:root {{ --fundo:#f6f4ef; --cartao:#fff; --texto:#1f2421; --suave:#5f6b64; --marca:#1f6b4a; --linha:#e3e0d8; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --fundo:#141816; --cartao:#1d2320; --texto:#e8ece9; --suave:#9aa69f; --marca:#5fc596; --linha:#2c3430; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--fundo); color:var(--texto); font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
header {{ max-width:860px; margin:0 auto; padding:32px 16px 8px; }}
h1 {{ margin:0; font-size:28px; color:var(--marca); }}
header p {{ margin:4px 0 0; color:var(--suave); }}
nav {{ max-width:860px; margin:16px auto 0; padding:0 16px; display:flex; flex-wrap:wrap; gap:8px; }}
nav button {{ border:1px solid var(--linha); background:var(--cartao); color:var(--texto); border-radius:999px; padding:6px 14px; font:inherit; font-size:14px; cursor:pointer; }}
nav button.ativo {{ background:var(--marca); border-color:var(--marca); color:#fff; }}
main {{ max-width:860px; margin:0 auto; padding:16px; display:grid; gap:12px; }}
.cartao {{ background:var(--cartao); border:1px solid var(--linha); border-radius:12px; padding:16px 18px; }}
.cartao.destaque {{ border-left:4px solid var(--marca); }}
.meta {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; font-size:13px; color:var(--suave); }}
.tema {{ font-weight:600; color:var(--marca); }}
.aviso {{ font-size:12px; border:1px solid var(--linha); border-radius:6px; padding:0 6px; }}
h2 {{ font-size:18px; margin:6px 0; line-height:1.35; }}
h2 a {{ color:inherit; text-decoration:none; }}
h2 a:hover {{ text-decoration:underline; }}
.cartao p {{ margin:0; color:var(--texto); }}
footer {{ max-width:860px; margin:0 auto; padding:16px 16px 40px; font-size:13px; color:var(--suave); }}
footer ul {{ padding-left:18px; }}
</style>
</head>
<body>
<header>
  <h1>{html.escape(NOME_NEWSLETTER)}</h1>
  <p>{html.escape(SUBTITULO)}</p>
  <p>Última atualização: {atualizado} (horário de Brasília)</p>
</header>
<nav><button class="ativo" data-filtro="">Todos</button>{botoes}</nav>
<main>
{cartoes}
</main>
<footer>
  <p>Resumos gerados automaticamente a partir dos portais citados. Sempre confira a matéria original no link.</p>
  <details><summary>Situação das fontes nesta rodada</summary><ul>{linhas_status}</ul></details>
</footer>
<script>
document.querySelectorAll("nav button").forEach(function (b) {{
  b.addEventListener("click", function () {{
    document.querySelectorAll("nav button").forEach(function (x) {{ x.classList.remove("ativo"); }});
    b.classList.add("ativo");
    var f = b.getAttribute("data-filtro");
    document.querySelectorAll(".cartao").forEach(function (c) {{
      c.style.display = (!f || c.getAttribute("data-tema") === f) ? "" : "none";
    }});
  }});
}});
</script>
</body>
</html>"""
    SITE.mkdir(exist_ok=True)
    (SITE / "index.html").write_text(pagina, encoding="utf-8")
    (SITE / ".nojekyll").write_text("", encoding="utf-8")


# ---------- rodada completa ----------
def main():
    fontes, temas = ler_fontes(), ler_temas()
    historico = ler_json(ARQ_NOTICIAS, [])
    cache_feeds = ler_json(ARQ_FEEDS, {})
    conhecidos = {n["id"] for n in historico}
    titulos = {normalizar(n["titulo"]) for n in historico}

    novas, status = coletar(fontes, cache_feeds)
    adicionadas = 0
    for m in novas:
        chave_titulo = normalizar(m["titulo"])
        if m["id"] in conhecidos or chave_titulo in titulos:
            continue
        achados = temas_por_palavra(m, temas)
        if not achados:
            continue
        m["tema"], m["ia"], m["relevancia"] = achados[0], False, 0
        m["resumo"] = m["trecho"]
        historico.append(m)
        conhecidos.add(m["id"])
        titulos.add(chave_titulo)
        adicionadas += 1
    print(f"{adicionadas} matérias novas dentro dos temas.")

    # manda ao Gemini as matérias que ainda não têm resumo da IA (mais recentes primeiro)
    pendentes = sorted((n for n in historico if not n.get("ia") and n.get("tema")),
                       key=lambda n: n["data"], reverse=True)
    aplicar_ia(pendentes, temas)

    limite = AGORA - timedelta(days=DIAS_NO_HISTORICO)
    historico = [n for n in historico if datetime.fromisoformat(n["data"]) >= limite]
    salvar_json(ARQ_NOTICIAS, historico)
    salvar_json(ARQ_FEEDS, cache_feeds)
    montar_pagina(historico, temas, status)
    print("Página pronta em site/index.html")


if __name__ == "__main__":
    main()
