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
import shutil

# ---------------- CONFIGURAÇÃO ----------------
NOME_NEWSLETTER = "Mimosa News"
ASSINATURA = "Radar de Ingredientes · por Rafael Almeida"
SUBTITULO = "Mercado, safra e cotações dos principais ingredientes da indústria, filtrados por quem vive o setor."
DIAS_NA_PAGINA = 7          # notícias dos últimos N dias aparecem na página
DIAS_NO_HISTORICO = 30      # quanto tempo o robô lembra das notícias
MAX_IA_POR_RODADA = 90      # máximo de matérias enviadas ao Gemini por rodada
LOTE_IA = 15                # matérias por chamada ao Gemini
PAUSA_ENTRE_CHAMADAS = 13   # segundos entre chamadas (respeita o limite por minuto do plano grátis)
VERSAO_IA = 2              # sobe quando mudamos o que a IA escreve; faz ela reprocessar
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
# Formato que o Gemini é obrigado a seguir (nenhum campo pode faltar)
FORMATO_RESPOSTA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "i": {"type": "INTEGER"},
            "tema": {"type": "STRING"},
            "resumo": {"type": "STRING"},
            "relevancia": {"type": "INTEGER"},
            "ia_tipo": {"type": "STRING", "enum": ["insight", "alerta", "sugestao", "contexto"]},
            "ia_texto": {"type": "STRING"},
        },
        "required": ["i", "tema", "resumo", "relevancia", "ia_tipo", "ia_texto"],
    },
}


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
1. Escolha UM tema da lista ({", ".join(nomes)}) ou "fora" se a notícia não tiver relação real com o foco dos temas. Use "Notícias do setor" para lançamentos, tecnologias, tendências e movimentos de empresas que não sejam sobre preço ou safra de um ingrediente.
2. Escreva um resumo em português, com 2 frases curtas, só com fatos que estão no título e no trecho. Não invente números, não opine e não copie frases inteiras: reescreva com suas palavras.
3. Dê uma nota de relevância de 1 a 5 para quem compra e vende esses ingredientes.
4. Escreva um comentário da IA ("ia_texto"), com 1 ou 2 frases, que complemente a notícia para um vendedor de ingredientes: o que isso pode significar para preço, oferta ou demanda, o que vale acompanhar, uma oportunidade de conversa com clientes ou um cuidado. Pode usar conhecimento geral de mercado, mas não invente números, datas, nomes de empresas ou links, e escreva como possibilidade ("pode", "vale acompanhar"), nunca como certeza.
5. Classifique o comentário em "ia_tipo": "insight" (leitura de mercado), "alerta" (risco ou ponto de atenção), "sugestao" (ação prática) ou "contexto" (explica um termo ou o pano de fundo).

Responda só com JSON neste formato:
[{{"i": 0, "tema": "...", "resumo": "...", "relevancia": 3, "ia_tipo": "insight", "ia_texto": "..."}}]

Notícias:
{itens}"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODELO}:generateContent"
    corpo = {
        "contents": [{"parts": [{"text": instrucao}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2,
                             "responseSchema": FORMATO_RESPOSTA},
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
            tipo = normalizar(str(resp.get("ia_tipo", "")))
            m["ia_tipo"] = tipo if tipo in ("insight", "alerta", "sugestao", "contexto") else "insight"
            m["ia_texto"] = str(resp.get("ia_texto") or "").strip()
            m["ia"] = True
            if m["ia_texto"]:
                m["versao_ia"] = VERSAO_IA  # pronta; sem comentário, tenta de novo na próxima rodada
        if inicio + LOTE_IA < len(pendentes):
            time.sleep(PAUSA_ENTRE_CHAMADAS)


# ---------- página ----------
# Cor de cada ingrediente: (modo claro, modo escuro). Temas novos usam as cores de reserva.
CORES_TEMAS = {
    "acucar": ("#B8901A", "#E2C05A"),
    "trigo e farinhas": ("#B0662A", "#E39A5C"),
    "arroz": ("#4F8A3C", "#8CC774"),
    "oleos e gorduras": ("#7D7A12", "#C7C34F"),
    "cacau": ("#6B3B22", "#C48862"),
    "lacteos": ("#3473AD", "#7DB3E6"),
    "coco": ("#8A6A45", "#C9A57A"),
    "noticias do setor": ("#5B5F97", "#A3A7E0"),
}
CORES_RESERVA = [("#5B5F97", "#A3A7E0"), ("#9C4F6B", "#E59AB5"), ("#2F7F7A", "#79C9C3")]


def slug(texto):
    return re.sub(r"[^a-z0-9]+", "-", normalizar(texto)).strip("-")


def nome_do_dia(data_local):
    hoje = AGORA.astimezone(FUSO).date()
    if data_local == hoje:
        return "Hoje"
    if data_local == hoje - timedelta(days=1):
        return "Ontem"
    dias = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
    return f"{dias[data_local.weekday()].capitalize()}, {data_local:%d/%m}"


MASCOTE_SVG = """<svg class="vaca" viewBox="0 0 240 250" aria-hidden="true">
<defs><clipPath id="vc-cab"><ellipse cx="120" cy="112" rx="72" ry="62"/></clipPath>
<clipPath id="vc-corpo"><path d="M38 250 C38 196 70 172 120 172 C170 172 202 196 202 250 Z"/></clipPath></defs>
<g stroke="#3B2A20" stroke-width="3.5" stroke-linejoin="round" stroke-linecap="round">
  <path d="M38 250 C38 196 70 172 120 172 C170 172 202 196 202 250 Z" fill="#FFFFFF"/>
  <g clip-path="url(#vc-corpo)" stroke="none"><path d="M160 178 C186 190 200 214 204 250 L156 250 C146 228 146 200 160 178Z" fill="#3B2A20"/><path d="M40 222 C58 212 74 222 70 250 L36 250Z" fill="#3B2A20"/></g>
  <path d="M38 250 C38 196 70 172 120 172 C170 172 202 196 202 250" fill="none"/>
  <path d="M84 176 Q120 194 156 176" fill="none" stroke="#C8473D" stroke-width="8"/>
  <path d="M110 188 h20 l3 14 a13 13 0 0 1 -26 0 z" fill="#F2B632"/>
  <ellipse cx="44" cy="96" rx="30" ry="14" transform="rotate(-18 44 96)" fill="#FFFFFF"/>
  <ellipse cx="47" cy="97" rx="17" ry="7" transform="rotate(-18 47 97)" fill="#F7A9B9" stroke="none"/>
  <ellipse cx="196" cy="96" rx="30" ry="14" transform="rotate(18 196 96)" fill="#FFFFFF"/>
  <ellipse cx="193" cy="97" rx="17" ry="7" transform="rotate(18 193 97)" fill="#F7A9B9" stroke="none"/>
  <path d="M66 82 C50 78 40 66 40 52 C54 58 64 64 78 70" fill="#FFF1CF"/>
  <path d="M174 82 C190 78 200 66 200 52 C186 58 176 64 162 70" fill="#FFF1CF"/>
  <ellipse cx="120" cy="112" rx="72" ry="62" fill="#FFFFFF"/>
  <g clip-path="url(#vc-cab)" stroke="none">
    <path d="M44 84 C66 66 92 76 88 102 C84 124 58 126 44 114Z" fill="#3B2A20"/>
    <path d="M152 58 C174 60 194 74 196 98 C178 100 160 86 152 58Z" fill="#3B2A20"/>
  </g>
  <ellipse cx="120" cy="112" rx="72" ry="62" fill="none"/>
  <ellipse cx="92" cy="106" rx="13" ry="17" fill="#2B1D16" stroke="none"/>
  <ellipse cx="148" cy="106" rx="13" ry="17" fill="#2B1D16" stroke="none"/>
  <circle cx="97" cy="99" r="5.5" fill="#FFFFFF" stroke="none"/>
  <circle cx="153" cy="99" r="5.5" fill="#FFFFFF" stroke="none"/>
  <circle cx="88" cy="113" r="2.5" fill="#FFFFFF" stroke="none"/>
  <circle cx="144" cy="113" r="2.5" fill="#FFFFFF" stroke="none"/>
  <path d="M78 86 q12 -7 24 -2" fill="none" stroke-width="3"/>
  <path d="M138 84 q12 -5 24 2" fill="none" stroke-width="3"/>
  <ellipse cx="70" cy="132" rx="10" ry="6" fill="#F49AB0" opacity="0.75" stroke="none"/>
  <ellipse cx="170" cy="132" rx="10" ry="6" fill="#F49AB0" opacity="0.75" stroke="none"/>
  <ellipse cx="120" cy="148" rx="44" ry="25" fill="#F8C3CD"/>
  <ellipse cx="104" cy="145" rx="5" ry="7" fill="#3B2A20" stroke="none"/>
  <ellipse cx="136" cy="145" rx="5" ry="7" fill="#3B2A20" stroke="none"/>
  <path d="M110 160 Q120 168 130 160" fill="none"/>
  <ellipse cx="120" cy="58" rx="66" ry="13" fill="#F2C76B"/>
  <path d="M84 56 C82 24 158 24 156 56 Z" fill="#F6D58A"/>
  <path d="M85 47 C100 43 140 43 155 47 L156 55 C140 51 100 51 84 55Z" fill="#C8473D" stroke="none"/>
  <path d="M84 56 C82 24 158 24 156 56" fill="none"/>
  <g class="placa">
  <g transform="rotate(-5 120 230)">
    <rect x="66" y="210" width="108" height="58" rx="3" fill="#FFFDF7"/>
    <text x="120" y="228" text-anchor="middle" font-family="Arial, sans-serif" font-size="12" font-weight="700" fill="#3B2A20" stroke="none">RADAR</text>
    <path d="M78 238 h36 M78 246 h36 M126 238 h36 M126 246 h36" fill="none" stroke="#B9AFA6" stroke-width="3"/>
  </g>
  <ellipse cx="66" cy="232" rx="13" ry="11" fill="#6B5A50"/>
  <ellipse cx="174" cy="226" rx="13" ry="11" fill="#6B5A50"/>
  </g>
</g>
</svg>"""

ROTULOS_IA = {"insight": "Insight de mercado", "alerta": "Atenção", "sugestao": "Sugestão", "contexto": "Para entender"}

MODELO_PAGINA = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__NOME__ · __ASSINATURA__</title>
<meta name="description" content="__SUBTITULO__">
<link rel="manifest" href="manifest.json">
<meta name="theme-color" content="#BFE3F2">
<link rel="icon" type="image/png" sizes="192x192" href="icone-192.png">
<link rel="apple-touch-icon" href="icone-192.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Mimosa News">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible+Next:wght@400;500;700;800&display=swap" rel="stylesheet">
<style>
:root {
  --papel: #FFFFFF; --fundo: #F4F9F1; --tinta: #16211C; --apagado: #55625A;
  --linha: #DDE7DA; --foco: #1D5BD8; --zap: #1E8E4E;
  --ia-fundo: #EEF3FC; --ia-selo: #2D5BB9; --alerta-fundo: #FFF4E5; --alerta-selo: #B4561A;
}
@media (prefers-color-scheme: dark) {
  :root { --papel: #19201D; --fundo: #101513; --tinta: #E9EFEC; --apagado: #9AA8A1;
          --linha: #2B3531; --foco: #8AB4FF; --zap: #5FCB8A;
          --ia-fundo: #1D2735; --ia-selo: #6E9BF0; --alerta-fundo: #33261A; --alerta-selo: #E89A5C; }
  .tema-cor { --c: var(--cd) !important; }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }  /* o que está escondido fica escondido, sempre */
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--fundo); color: var(--tinta);
  font: 17px/1.55 "Atkinson Hyperlegible Next", system-ui, "Segoe UI", sans-serif; }
a { color: inherit; }
:focus-visible { outline: 3px solid var(--foco); outline-offset: 2px; border-radius: 4px; }
.miolo { max-width: 760px; margin: 0 auto; padding: 0 16px; }

/* cabeçalho ilustrado (fica igual no modo claro e escuro) */
.cena { position: relative; overflow: hidden; background: linear-gradient(180deg, #BFE3F2 0%, #E3F3F7 70%); }
.cena .sol { position: absolute; top: 18px; right: max(150px, calc(50% - 470px)); width: 76px; height: 76px; border-radius: 50%;
  background: #FFE08A; box-shadow: 0 0 0 14px rgba(255, 224, 138, .35); }
.cena .nuvens { position: absolute; top: 18px; left: 0; width: 100%; height: 90px; }
.cena .campo { position: absolute; left: 0; bottom: 0; width: 100%; height: 96px; }
.cena .vaca { position: absolute; bottom: 0; right: max(4px, calc(50% - 400px)); width: clamp(140px, 24vw, 226px); height: auto; }
.cena-texto { position: relative; padding-top: 36px; padding-bottom: 118px; }
.cena-texto h1, .cena-texto p { color: #16302A; max-width: 30ch; }
.cena-texto .atualizado { display: inline-block; margin-top: 10px; font-size: 14px; font-weight: 700; color: #16302A;
  background: rgba(255, 255, 255, .75); border-radius: 999px; padding: 3px 12px; }
.cena-texto .assinatura { margin: 6px 0 0; font-size: 17px; font-weight: 700; color: #2B5A3F; max-width: none; }
.cena-texto .subtitulo { color: #2E4A43; margin-top: 8px; }
.vaca .placa { transform-box: fill-box; transform-origin: 50% 100%; animation: balanca 2.8s ease-in-out infinite alternate; }
@keyframes balanca { from { transform: rotate(-7deg); } to { transform: rotate(7deg); } }
/* vento leve: nuvens e sol se movem devagar para os lados */
.nuvem-1 { animation: vento 11s ease-in-out infinite alternate; }
.nuvem-2 { animation: vento 14s ease-in-out -5s infinite alternate-reverse; }
@keyframes vento { from { transform: translateX(-28px); } to { transform: translateX(28px); } }
.cena .sol { animation: sol-vento 9s ease-in-out infinite alternate; }
@keyframes sol-vento { from { transform: translateX(-7px); } to { transform: translateX(7px); } }
@media (prefers-reduced-motion: reduce) { .vaca .placa, .nuvem, .cena .sol { animation: none; } }
@media (max-width: 600px) {
  .cena .sol { top: auto; right: auto; left: 40px; bottom: 58px; width: 58px; height: 58px; }
  .cena-texto { padding-bottom: 150px; }
  .cena-texto h1, .cena-texto p { max-width: 100%; }
}
.topo-busca { padding-top: 20px; }
.instalar { display: flex; align-items: center; gap: 14px; margin-top: 16px; padding: 12px 14px;
  background: var(--papel); border: 1px solid var(--linha); border-radius: 14px; }
.instalar img { width: 48px; height: 48px; border-radius: 12px; flex: none; }
.instalar .txt { flex: 1; font-size: 15px; line-height: 1.4; }
.instalar strong { display: block; font-size: 16px; }
.instalar .botoes { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
.instalar button { font: inherit; font-size: 15px; font-weight: 700; border-radius: 999px; cursor: pointer; padding: 7px 14px; }
.instalar .sim { background: var(--tinta); color: var(--papel); border: 0; }
.instalar .nao { background: none; border: 0; color: var(--apagado); padding: 7px 6px; }
@media (max-width: 520px) { .instalar { flex-wrap: wrap; } .instalar .botoes { width: 100%; } }
.offline { margin-top: 12px; padding: 8px 12px; border-radius: 10px; background: var(--alerta-fundo); font-size: 15px; }
.ia { display: flex; gap: 10px; align-items: flex-start; margin-top: 12px; padding: 10px 12px; border-radius: 10px;
  background: var(--ia-fundo); font-size: 16px; max-width: 68ch; }
.ia .selo { flex: none; margin-top: 2px; font-size: 12px; font-weight: 800; color: #fff; background: var(--ia-selo);
  border-radius: 6px; padding: 1px 7px; }
.ia.alerta { background: var(--alerta-fundo); }
.ia.alerta .selo { background: var(--alerta-selo); }
@media (prefers-color-scheme: dark) { .ia .selo { color: #10151A; } }
.titulo { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 4px 16px; }
h1 { margin: 0; font-size: clamp(30px, 6.4vw, 46px); font-weight: 800; letter-spacing: -0.02em; line-height: 1.1; }
.atualizado { color: var(--apagado); font-size: 15px; }
.subtitulo { margin: 8px 0 0; color: var(--apagado); max-width: 60ch; }

.busca { display: block; position: relative; }
.busca input { width: 100%; font: inherit; color: var(--tinta); background: var(--papel);
  border: 1px solid var(--linha); border-radius: 10px; padding: 12px 14px 12px 42px; }
.busca svg { position: absolute; left: 14px; top: 50%; transform: translateY(-50%); color: var(--apagado); }

.faixa-wrap { position: sticky; top: 0; z-index: 5; background: var(--fundo);
  border-bottom: 1px solid var(--linha); }
.faixa { display: flex; gap: 8px; overflow-x: auto; padding: 12px 16px; max-width: 760px; margin: 0 auto;
  scrollbar-width: thin; }
.chip { flex: 0 0 auto; display: flex; align-items: center; gap: 8px; font: inherit; font-size: 15px;
  color: var(--tinta); background: var(--papel); border: 1px solid var(--linha); border-radius: 999px;
  padding: 7px 14px 7px 10px; cursor: pointer; }
.chip .ponto { width: 12px; height: 12px; border-radius: 50%; background: var(--c); }
.chip .qtd { font-weight: 800; font-variant-numeric: tabular-nums; }
.chip.zero .qtd { color: var(--apagado); font-weight: 500; }
.chip[aria-pressed="true"] { background: var(--tinta); color: var(--papel); border-color: var(--tinta); }
.chip[aria-pressed="true"] .qtd { color: var(--papel); }
.legenda.miolo { margin-top: 10px; margin-bottom: 0; font-size: 14px; color: var(--apagado); }
@media (min-width: 700px) { .faixa { flex-wrap: wrap; overflow: visible; } .faixa-wrap { position: static; border-bottom: 0; } }

main.miolo { padding-top: 0; padding-bottom: 24px; }
.dia h2 { font-size: 15px; font-weight: 700; color: var(--apagado); margin: 24px 0 8px; }
.folha { background: var(--papel); border: 1px solid var(--linha); border-radius: 14px; overflow: hidden; }
.item { position: relative; padding: 18px 20px 16px 24px; border-top: 1px solid var(--linha); }
.item:first-child { border-top: 0; }
.item::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 6px; background: var(--c); }
.topo { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; font-size: 14px; }
.ingrediente { font-weight: 700; color: var(--c); }
.etiqueta { font-size: 13px; font-weight: 700; border-radius: 6px; padding: 1px 8px; }
.etiqueta.importante { border: 1.5px solid var(--c); color: var(--tinta); }
.etiqueta.nova { border: 1.5px solid var(--foco); color: var(--foco); display: none; }
.item.e-nova .etiqueta.nova { display: inline-block; }
.etiqueta.portal { color: var(--apagado); border: 1px solid var(--linha); font-weight: 500; }
.item h3 { margin: 6px 0 6px; font-size: 20px; line-height: 1.3; font-weight: 700; }
.item h3 a { text-decoration: none; }
.item h3 a:hover { text-decoration: underline; text-underline-offset: 3px; }
.item p { margin: 0; max-width: 68ch; }
.rodape-item { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; margin-top: 12px;
  font-size: 15px; color: var(--apagado); }
.rodape-item .fonte { margin-right: auto; }
.acao { display: inline-flex; align-items: center; gap: 6px; font-weight: 700; text-decoration: none;
  padding: 6px 2px; color: var(--tinta); }
.acao.zap { color: var(--zap); }
.acao:hover { text-decoration: underline; text-underline-offset: 3px; }

.vazio { background: var(--papel); border: 1px dashed var(--linha); border-radius: 14px;
  padding: 28px 20px; text-align: center; color: var(--apagado); margin-top: 24px; }
.vazio button { font: inherit; font-weight: 700; color: var(--foco); background: none; border: 0;
  cursor: pointer; text-decoration: underline; }

footer.miolo { padding-top: 24px; padding-bottom: 56px; font-size: 14px; color: var(--apagado); }
footer summary { cursor: pointer; }
footer ul { padding-left: 18px; }
@media (max-width: 520px) {
  .item { padding: 16px 16px 14px 20px; }
  .item h3 { font-size: 18px; }
  .rodape-item .fonte { flex-basis: 100%; }
}
</style>
</head>
<body>
<header class="cena">
  <div class="sol" aria-hidden="true"></div>
  <svg class="nuvens" viewBox="0 0 1200 90" preserveAspectRatio="xMidYMin slice" aria-hidden="true">
    <g fill="#FFFFFF" opacity="0.9">
      <g class="nuvem nuvem-1"><ellipse cx="160" cy="44" rx="58" ry="16"/><ellipse cx="190" cy="34" rx="34" ry="18"/></g>
      <g class="nuvem nuvem-2"><ellipse cx="930" cy="30" rx="48" ry="13"/><ellipse cx="952" cy="22" rx="26" ry="14"/></g>
    </g>
  </svg>
  <svg class="campo" viewBox="0 0 1200 140" preserveAspectRatio="none" aria-hidden="true">
    <path d="M0 64 C200 26 380 36 560 64 C760 96 980 34 1200 54 L1200 140 L0 140Z" fill="#B5DA92"/>
    <path d="M0 100 C240 76 460 88 700 104 C900 116 1060 92 1200 96 L1200 140 L0 140Z" fill="#F0D37E"/>
    <path d="M0 124 C300 114 700 120 1200 116 L1200 140 L0 140Z" fill="#E2B95A"/>
  </svg>
  __MASCOTE__
  <div class="miolo cena-texto">
    <h1>__NOME__</h1>
    <p class="assinatura">__ASSINATURA__</p>
    <p class="subtitulo">__SUBTITULO__</p>
    <span class="atualizado">Atualizado __ATUALIZADO__</span>
  </div>
</header>
<div class="miolo topo-busca">
  <div class="offline" id="offline" hidden>Você está sem internet. Mostrando as notícias da última vez que o app abriu.</div>
  <div class="instalar" id="instalar" hidden>
    <img src="icone-192.png" alt="">
    <div class="txt"><strong>Instale a Mimosa News</strong><span id="instalar-texto">Tenha as notícias na tela inicial do celular, como um app.</span></div>
    <div class="botoes"><button type="button" class="sim" id="instalar-sim">Instalar app</button><button type="button" class="nao" id="instalar-nao">Agora não</button></div>
  </div>
  <label class="busca" style="margin-top:16px">
    <span style="position:absolute;left:-9999px">Buscar notícia</span>
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
    <input id="busca" type="search" placeholder="Buscar notícia (ex.: Cepea, EUDR, Argentina)" autocomplete="off">
  </label>
</div>
<div class="faixa-wrap">
  <nav class="faixa" aria-label="Filtrar por ingrediente">
    <button class="chip" data-filtro="" aria-pressed="true"><span>Todos</span><span class="qtd">__TOTAL24__</span></button>
    __CHIPS__
  </nav>
</div>
<p class="legenda miolo">Os números mostram quantas notícias chegaram nas últimas 24 horas.</p>
<main class="miolo">
__DIAS__
<div class="vazio" id="vazio" hidden>
  <p id="vazio-texto">Nenhuma notícia encontrada.</p>
  <button type="button" id="limpar">Ver todas as notícias</button>
</div>
</main>
<footer class="miolo">
  <p>Os resumos e os comentários da IA são feitos automaticamente a partir das matérias dos portais. Os comentários são sugestões para orientar a leitura, não recomendações de compra ou venda. Para decisões, confira sempre a matéria original.</p>
  <details><summary>De onde vêm as notícias</summary><ul>__STATUS__</ul></details>
</footer>
<script>
// ----- app instalável -----
(function () {
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () { navigator.serviceWorker.register("sw.js").catch(function () {}); });
  }
  var caixa = document.getElementById("instalar");
  var texto = document.getElementById("instalar-texto");
  var sim = document.getElementById("instalar-sim");
  var off = document.getElementById("offline");
  function atualizarOffline() { off.hidden = navigator.onLine; }
  window.addEventListener("online", atualizarOffline); window.addEventListener("offline", atualizarOffline); atualizarOffline();

  var instalado = window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
  var dispensado = false;
  try { dispensado = Date.now() - Number(localStorage.getItem("mimosa-instalar-dispensado") || 0) < 14 * 864e5; } catch (e) {}
  if (instalado || dispensado) return;

  var pedido = null;
  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault(); pedido = e; caixa.hidden = false;
  });
  sim.addEventListener("click", function () {
    if (pedido) { pedido.prompt(); pedido.userChoice.finally(function () { caixa.hidden = true; pedido = null; }); }
  });
  document.getElementById("instalar-nao").addEventListener("click", function () {
    caixa.hidden = true;
    try { localStorage.setItem("mimosa-instalar-dispensado", String(Date.now())); } catch (e) {}
  });
  window.addEventListener("appinstalled", function () { caixa.hidden = true; });

  // iPhone e iPad: o Safari não tem botão de instalar, então mostramos o caminho
  var ios = /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  if (ios) {
    texto.textContent = "No Safari, toque em Compartilhar (o quadrado com a seta) e depois em \u201cAdicionar à Tela de Início\u201d.";
    sim.hidden = true; caixa.hidden = false;
  }
})();
</script>
<script>
(function () {
  var itens = Array.prototype.slice.call(document.querySelectorAll(".item"));
  var dias = Array.prototype.slice.call(document.querySelectorAll(".dia"));
  var chips = Array.prototype.slice.call(document.querySelectorAll(".chip"));
  var busca = document.getElementById("busca");
  var vazio = document.getElementById("vazio");
  var vazioTexto = document.getElementById("vazio-texto");
  var filtro = decodeURIComponent((location.hash || "").slice(1));

  function sem(t) { return (t || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, ""); }

  // marca como "nova" o que chegou depois da última visita
  try {
    var ultima = Number(localStorage.getItem("radar-ultima-visita") || 0);
    if (ultima) itens.forEach(function (i) { if (Number(i.dataset.ts) > ultima) i.classList.add("e-nova"); });
    localStorage.setItem("radar-ultima-visita", String(Date.now()));
  } catch (e) {}

  function aplicar() {
    var q = sem(busca.value.trim());
    var visiveis = 0;
    itens.forEach(function (i) {
      var ok = (!filtro || i.dataset.tema === filtro) && (!q || sem(i.textContent).indexOf(q) > -1);
      i.hidden = !ok; if (ok) visiveis++;
    });
    dias.forEach(function (d) { d.hidden = !d.querySelector(".item:not([hidden])"); });
    chips.forEach(function (c) { c.setAttribute("aria-pressed", String(c.dataset.filtro === filtro)); });
    vazio.hidden = visiveis > 0;
    if (!visiveis) {
      var nome = filtro ? document.querySelector('.chip[data-filtro="' + filtro + '"] span').textContent : "";
      vazioTexto.textContent = q ? "Nenhuma notícia com “" + busca.value.trim() + "”" + (nome ? " em " + nome : "") + "."
                                 : "Nenhuma notícia de " + nome + " nos últimos 7 dias.";
    }
  }
  chips.forEach(function (c) {
    c.addEventListener("click", function () {
      filtro = c.dataset.filtro;
      history.replaceState(null, "", filtro ? "#" + filtro : location.pathname);
      aplicar();
    });
  });
  busca.addEventListener("input", aplicar);
  document.getElementById("limpar").addEventListener("click", function () {
    filtro = ""; busca.value = ""; history.replaceState(null, "", location.pathname); aplicar();
  });
  window.addEventListener("hashchange", function () { filtro = decodeURIComponent(location.hash.slice(1)); aplicar(); });
  aplicar();
})();
</script>
</body>
</html>
"""


def montar_pagina(noticias, temas, status):
    corte = AGORA - timedelta(days=DIAS_NA_PAGINA)
    visiveis = [n for n in noticias if n.get("tema") and datetime.fromisoformat(n["data"]) >= corte]
    visiveis.sort(key=lambda n: n["data"], reverse=True)

    cores, reserva = {}, iter(CORES_RESERVA * 5)
    for t in temas:
        cores[t["nome"]] = CORES_TEMAS.get(normalizar(t["nome"])) or next(reserva)

    def estilo_cor(tema):
        claro, escuro = cores.get(tema, ("#56635C", "#9AA8A1"))
        return f'style="--c:{claro};--cd:{escuro}"'

    def item(n):
        data = datetime.fromisoformat(n["data"])
        local = data.astimezone(FUSO)
        resumo = encurtar(n.get("resumo") or n["trecho"] or "", 420)
        etiquetas = '<span class="etiqueta nova">nova</span>'
        if n.get("relevancia", 0) >= 4:
            etiquetas += '<span class="etiqueta importante">Importante</span>'
        if not n.get("ia"):
            etiquetas += '<span class="etiqueta portal">resumo do portal</span>'
        ia_html = ""
        if n.get("ia_texto"):
            tipo = n.get("ia_tipo", "insight")
            ia_html = (f'<div class="ia{" alerta" if tipo == "alerta" else ""}"><span class="selo">IA</span>'
                       f'<div><strong>{ROTULOS_IA.get(tipo, "Insight de mercado")}.</strong> {html.escape(n["ia_texto"])}</div></div>')
        texto_zap = f"{n['titulo']}\n\n{resumo}\n\n{n['link']}"
        zap = "https://wa.me/?text=" + quote_plus(texto_zap)
        return f"""<article class="item tema-cor" data-tema="{slug(n['tema'])}" data-ts="{int(data.timestamp() * 1000)}" {estilo_cor(n['tema'])}>
  <div class="topo"><span class="ingrediente">{html.escape(n['tema'])}</span>{etiquetas}</div>
  <h3><a href="{html.escape(n['link'])}" target="_blank" rel="noopener">{html.escape(n['titulo'])}</a></h3>
  <p>{html.escape(resumo)}</p>
  {ia_html}
  <div class="rodape-item">
    <span class="fonte">{html.escape(n['fonte'])}, {local:%H:%M}</span>
    <a class="acao" href="{html.escape(n['link'])}" target="_blank" rel="noopener">Ler matéria</a>
    <a class="acao zap" href="{html.escape(zap)}" target="_blank" rel="noopener">Enviar no WhatsApp</a>
  </div>
</article>"""

    # agrupa por dia
    grupos = []
    for n in visiveis:
        dia = datetime.fromisoformat(n["data"]).astimezone(FUSO).date()
        if not grupos or grupos[-1][0] != dia:
            grupos.append((dia, []))
        grupos[-1][1].append(n)
    blocos = "\n".join(
        f'<section class="dia"><h2>{nome_do_dia(dia)}</h2><div class="folha">'
        + "\n".join(item(n) for n in lista) + "</div></section>"
        for dia, lista in grupos
    )

    limite24 = AGORA - timedelta(hours=24)
    def qtd24(nome=None):
        return sum(1 for n in visiveis if datetime.fromisoformat(n["data"]) >= limite24
                   and (nome is None or n["tema"] == nome))

    chips = "".join(
        f'<button class="chip tema-cor{" zero" if qtd24(t["nome"]) == 0 else ""}" data-filtro="{slug(t["nome"])}" '
        f'aria-pressed="false" {estilo_cor(t["nome"])}><span class="ponto" aria-hidden="true"></span>'
        f'<span>{html.escape(t["nome"])}</span><span class="qtd">{qtd24(t["nome"])}</span></button>'
        for t in temas
    )
    linhas_status = "".join(
        f"<li>{html.escape(s['fonte'])}: {s['materias']} matérias ({html.escape(s['metodo'])})</li>"
        for s in status
    )
    agora_local = AGORA.astimezone(FUSO)
    atualizado = f"hoje às {agora_local:%H:%M}"

    pagina = (MODELO_PAGINA
              .replace("__MASCOTE__", MASCOTE_SVG)
              .replace("__NOME__", html.escape(NOME_NEWSLETTER))
              .replace("__ASSINATURA__", html.escape(ASSINATURA))
              .replace("__SUBTITULO__", html.escape(SUBTITULO))
              .replace("__ATUALIZADO__", atualizado)
              .replace("__TOTAL24__", str(qtd24()))
              .replace("__CHIPS__", chips)
              .replace("__DIAS__", blocos)
              .replace("__STATUS__", linhas_status))
    SITE.mkdir(exist_ok=True)
    (SITE / "index.html").write_text(pagina, encoding="utf-8")
    (SITE / ".nojekyll").write_text("", encoding="utf-8")
    montar_app()


def montar_app():
    """Arquivos que transformam a página num app instalável (PWA)."""
    manifesto = {
        "name": NOME_NEWSLETTER,
        "short_name": NOME_NEWSLETTER,
        "description": SUBTITULO,
        "lang": "pt-BR",
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "background_color": "#F4F9F1",
        "theme_color": "#BFE3F2",
        "icons": [
            {"src": "icone-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "icone-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "icone-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    (SITE / "manifest.json").write_text(json.dumps(manifesto, ensure_ascii=False, indent=1), encoding="utf-8")
    versao = AGORA.strftime("%Y%m%d%H%M")
    (SITE / "sw.js").write_text(SERVICE_WORKER.replace("__VERSAO__", versao), encoding="utf-8")
    for icone in ("icone-192.png", "icone-512.png"):
        if (BASE / icone).exists():
            shutil.copy(BASE / icone, SITE / icone)
        else:
            print(f"Aviso: {icone} não encontrado no repositório; o app ficará sem ícone.")


# O "service worker" guarda uma cópia da página para abrir sem internet.
# Sempre tenta a versão nova primeiro; só usa a cópia guardada se estiver offline.
SERVICE_WORKER = """const CACHE = "mimosa-__VERSAO__";
const ARQUIVOS = ["./", "./index.html", "./manifest.json", "./icone-192.png", "./icone-512.png"];
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ARQUIVOS)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET" || new URL(req.url).origin !== self.location.origin) return;
  e.respondWith(
    fetch(req).then((resp) => {
      const copia = resp.clone();
      caches.open(CACHE).then((c) => c.put(req, copia));
      return resp;
    }).catch(() => caches.match(req).then((r) => r || caches.match("./index.html")))
  );
});
"""


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
    pendentes = sorted((n for n in historico
                        if n.get("tema") and (n.get("versao_ia") != VERSAO_IA or not n.get("ia_texto"))),
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
