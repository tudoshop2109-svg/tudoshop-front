"""Coleta diária dos produtos mais vendidos da Shopee para os painéis
"Em alta na semana" e "Em alta no mês" da aba Caçar ofertas.

Roda todo dia pelo GitHub Actions (.github/workflows/tendencias.yml):
1. pede ao backend da Tudo Shop as listas de mais vendidos (geral e por segmento);
2. guarda a foto do dia em dados/historico/AAAA-MM-DD.json;
3. compara com os dias anteriores e grava dados/tendencias.json, que a página lê.

A Shopee só informa o total de vendas de cada produto (desde sempre). Por isso
as vendas da semana e do mês são a diferença entre o total de hoje e o total
de 7 e 30 dias atrás. Até completar esses dias, o ranking usa a diferença
desde o primeiro dia coletado, ou o total de vendas no primeiro dia.

Só usa a biblioteca padrão do Python. Para rodar à mão:
    python scripts/coletar_tendencias.py
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BACKEND = "https://tudoshop-backend.vercel.app/api/search"
RAIZ = Path(__file__).resolve().parent.parent
PASTA_DADOS = RAIZ / "dados"
PASTA_HISTORICO = PASTA_DADOS / "historico"
BRASILIA = timezone(timedelta(hours=-3))
DIAS_GUARDADOS = 35  # um pouco mais que o mês, para sempre ter a base de 30 dias
TOP = 10

# Cada segmento junta algumas palavras-chave. "geral" (palavra None) usa a
# lista de mais vendidos da própria Shopee, sem palavra-chave.
SEGMENTOS = {
    "geral": {"nome": "Geral", "palavras": [None]},
    "casa": {"nome": "Casa", "palavras": ["cozinha", "organizador", "decoração"]},
    "beauty": {"nome": "Beauty", "palavras": ["maquiagem", "skincare", "cabelo"]},
    "tech": {"nome": "Tech", "palavras": ["fone bluetooth", "acessórios celular", "gamer"]},
    "style": {"nome": "Style", "palavras": ["bolsa feminina", "vestido", "tênis"]},
}

CAMPOS = ("itemId", "shopId", "name", "image", "newPrice", "oldPrice", "discount",
          "sales", "rating", "commission", "affiliateLink")


def buscar(palavra, tentativas=3):
    params = {"ordem": "vendidos", "limite": "50"}
    if palavra:
        params["q"] = palavra
    url = BACKEND + "?" + urllib.parse.urlencode(params)
    pedido = urllib.request.Request(url, headers={"User-Agent": "tudoshop-coletor"})
    for tentativa in range(1, tentativas + 1):
        try:
            with urllib.request.urlopen(pedido, timeout=60) as resposta:
                return json.load(resposta).get("offers", [])
        except urllib.error.HTTPError as erro:
            # 400 é pedido inválido (não adianta repetir); 5xx costuma ser a
            # Shopee limitando muitas buscas seguidas, então espera e tenta de novo.
            if erro.code < 500 or tentativa == tentativas:
                raise
            time.sleep(5 * tentativa)


def coletar():
    """Devolve {itemId: produto} com tudo o que apareceu hoje nas listas."""
    produtos = {}
    for seg_id, seg in SEGMENTOS.items():
        for palavra in seg["palavras"]:
            try:
                ofertas = buscar(palavra)
            except Exception as erro:  # uma lista falhar não derruba as outras
                print(f"Aviso: falhou {seg_id} / {palavra or 'ranking geral'}: {erro}", file=sys.stderr)
                continue
            for oferta in ofertas:
                produto = produtos.setdefault(oferta["itemId"], {"segmentos": []})
                produto.update({campo: oferta.get(campo) for campo in CAMPOS})
                if seg_id not in produto["segmentos"]:
                    produto["segmentos"].append(seg_id)
            time.sleep(2)  # pausa entre buscas para não esbarrar no limite da Shopee
    return produtos


def carregar_historico():
    """Devolve {"AAAA-MM-DD": {itemId: produto}} com as fotos guardadas."""
    return {
        arquivo.stem: json.loads(arquivo.read_text(encoding="utf-8"))
        for arquivo in sorted(PASTA_HISTORICO.glob("*.json"))
    }


def ranking(fotos, hoje, dias):
    """Ranking por segmento das vendas nos últimos `dias` dias."""
    limite = (date.fromisoformat(hoje) - timedelta(days=dias)).isoformat()
    anteriores = [d for d in fotos if limite <= d < hoje]
    base = min(anteriores) if anteriores else None
    dias_medidos = (date.fromisoformat(hoje) - date.fromisoformat(base)).days if base else 0
    atual = fotos[hoje]

    por_segmento = {}
    for seg_id in SEGMENTOS:
        itens = []
        for item_id, produto in atual.items():
            if seg_id not in produto.get("segmentos", []):
                continue
            vendas_periodo = None
            if base and item_id in fotos[base]:
                vendas_periodo = max(0, produto["sales"] - fotos[base][item_id]["sales"])
            item = {campo: produto.get(campo) for campo in CAMPOS}
            item["vendasPeriodo"] = vendas_periodo
            itens.append(item)

        if base:
            # Só entra quem já estava na lista do dia-base, senão não dá para medir.
            itens = [i for i in itens if i["vendasPeriodo"] is not None]
            itens.sort(key=lambda i: i["vendasPeriodo"], reverse=True)
        else:
            itens.sort(key=lambda i: i["sales"], reverse=True)
        por_segmento[seg_id] = itens[:TOP]

    return {
        "dias": dias,
        "diasMedidos": dias_medidos,
        "completo": dias_medidos >= dias,
        "desde": base,
        "segmentos": por_segmento,
    }


def main():
    agora = datetime.now(BRASILIA)
    hoje = agora.date().isoformat()

    produtos = coletar()
    if not produtos:
        print("Nenhuma lista respondeu; mantendo os dados de ontem.", file=sys.stderr)
        sys.exit(1)

    PASTA_HISTORICO.mkdir(parents=True, exist_ok=True)
    (PASTA_HISTORICO / f"{hoje}.json").write_text(
        json.dumps(produtos, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    corte = (agora.date() - timedelta(days=DIAS_GUARDADOS)).isoformat()
    for arquivo in PASTA_HISTORICO.glob("*.json"):
        if arquivo.stem < corte:
            arquivo.unlink()

    fotos = carregar_historico()
    tendencias = {
        "atualizado": agora.isoformat(timespec="minutes"),
        "diasColetados": len(fotos),
        "nomes": {seg_id: seg["nome"] for seg_id, seg in SEGMENTOS.items()},
        "semana": ranking(fotos, hoje, 7),
        "mes": ranking(fotos, hoje, 30),
    }
    (PASTA_DADOS / "tendencias.json").write_text(
        json.dumps(tendencias, ensure_ascii=False, indent=1), encoding="utf-8")

    contagem = ", ".join(f"{s}: {len(tendencias['semana']['segmentos'][s])}" for s in SEGMENTOS)
    print(f"{hoje}: {len(produtos)} produtos coletados ({contagem}).")


if __name__ == "__main__":
    main()
