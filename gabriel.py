import time
import datetime
from datetime import timedelta
import webbrowser
import os

# Supondo que as classes Motor, MotorIA e a conexão 'iq_principal' já existam no seu código
# Se for testar isolado, lembre-se de importar ou colar a classe Motor aqui.

class CatalogadorPainel:
    def __init__(self, api, pares, estrategias, timeframe=300):
        self.api = api
        self.pares = pares
        self.estrategias = estrategias
        self.timeframe = timeframe
        
    def simular_sessao(self, par, qtd_candles=48):
        """Simula o comportamento do bot nas últimas N velas"""
        candles = self.api.get_candles(par, self.timeframe, qtd_candles, time.time())
        if not candles or len(candles) < 15:
            return None
            
        historico_operacoes = []
        
        # Começamos do índice 10 para ter histórico suficiente para as estratégias
        for i in range(10, len(candles) - 1):
            velas_analise = candles[:i]
            proxima_vela = candles[i]
            
            # 1. O que o bot decidiria?
            sinais = {}
            for est in self.estrategias:
                predicao = Motor.analisar_sinal_unico(est, velas_analise)
                if predicao:
                    sinais[est] = predicao
            
            if not sinais:
                continue # Nenhuma estratégia deu sinal nesta vela
                
            # 2. Decisão por maioria (ou força, como no seu bot original)
            calls = sum(1 for s in sinais.values() if s == "call")
            puts = sum(1 for s in sinais.values() if s == "put")
            
            if calls == puts:
                continue # Empate, o bot não opera
                
            direcao_bot = "call" if calls > puts else "put"
            estrategias_vencedoras = [e for e, s in sinais.items() if s == direcao_bot]
            melhor_est = estrategias_vencedoras[0] # Simplificação para o painel
            
            # 3. Resultado real da próxima vela
            cor_real = "call" if proxima_vela['close'] > proxima_vela['open'] else "put"
            resultado = "WIN" if direcao_bot == cor_real else "LOSS"
            
            # Formatar horário da vela
            dt_vela = datetime.datetime.fromtimestamp(velas_analise[-1]['from'])
            hora_formatada = dt_vela.strftime("%H:%M")
            
            historico_operacoes.append({
                'hora': hora_formatada,
                'direcao': direcao_bot.upper(),
                'estrategia': melhor_est,
                'resultado': resultado
            })
            
        return historico_operacoes

    def analisar_estatisticas(self, historico):
        """Analisa o comportamento após sequências de LOSS"""
        stats = {
            'total_win': 0,
            'total_loss': 0,
            'apos_1_loss': {'win': 0, 'loss': 0, 'total': 0},
            'apos_2_loss': {'win': 0, 'loss': 0, 'total': 0},
            'apos_3_loss': {'win': 0, 'loss': 0, 'total': 0},
        }
        
        loss_seguidos = 0
        
        for op in historico:
            if op['resultado'] == 'WIN':
                stats['total_win'] += 1
                # Reseta contagem de loss, mas antes registramos se foi recuperação
                loss_seguidos = 0
            else:
                stats['total_loss'] += 1
                loss_seguidos += 1
                
                # Registra estatística de recuperação para o PRÓXIMO ciclo
                # (Se agora é LOSS, queremos saber o que aconteceu depois)
                # Na verdade, a lógica correta é: quando entramos em um estado de N losses, 
                # como foi o resultado da operação *seguinte*?
                
        # Segunda passada para calcular recuperação com precisão
        loss_seguidos = 0
        for i in range(len(historico)):
            op = historico[i]
            if op['resultado'] == 'LOSS':
                loss_seguidos += 1
            else:
                # Se foi WIN, verificamos quantos losses ele quebrou
                if loss_seguidos == 1:
                    stats['apos_1_loss']['win'] += 1
                    stats['apos_1_loss']['total'] += 1
                elif loss_seguidos == 2:
                    stats['apos_2_loss']['win'] += 1
                    stats['apos_2_loss']['total'] += 1
                elif loss_seguidos >= 3:
                    stats['apos_3_loss']['win'] += 1
                    stats['apos_3_loss']['total'] += 1
                
                loss_seguidos = 0 # Reseta
                
        # Ajuste para losses no final da sequência que não tiveram "próxima operação" no dataset
        # (Opcional, mas mantém a matemática mais limpa)
        
        return stats

    def gerar_decisao(self, stats):
        """Define o status do mercado baseado nas estatísticas"""
        # Regras de decisão personalizáveis
        regra_1_loss = stats['apos_1_loss']['total'] > 0 and (stats['apos_1_loss']['win'] / stats['apos_1_loss']['total']) >= 0.60
        regra_2_loss = stats['apos_2_loss']['total'] > 0 and (stats['apos_2_loss']['win'] / stats['apos_2_loss']['total']) >= 0.50
        regra_3_loss = stats['apos_3_loss']['total'] > 0 and (stats['apos_3_loss']['win'] / stats['apos_3_loss']['total']) < 0.40
        
        if stats['total_win'] / (stats['total_win'] + stats['total_loss']) >= 0.65 and regra_1_loss:
            return "🟢 LIBERADO", "MERCADO BOM PARA O BOT", "#10b981" # Verde
        elif regra_3_loss or (stats['total_loss'] > stats['total_win']):
            return "🔴 BLOQUEADO", "MERCADO RUIM / INSTÁVEL", "#ef4444" # Vermelho
        else:
            return "🟡 ATENÇÃO", "MERCADO INSTÁVEL, USE GERENCIAMENTO", "#f59e0b" # Amarelo

    def gerar_html(self, par, historico, stats, decisao, cor_status):
        """Gera o código HTML do painel"""
        status_texto, conclusao, cor_bg = decisao
        
        # Gera as linhas do histórico
        linhas_historico = ""
        for op in historico[-12:]: # Mostra as últimas 12 operações para não poluir
            emoji = "🟢" if op['resultado'] == "WIN" else "🔴"
            linhas_historico += f"""
            <div class="op-row">
                <span class="op-time">{op['hora']}</span>
                <span class="op-dir">{op['direcao']}</span>
                <span class="op-est">{op['estrategia']}</span>
                <span class="op-res {op['resultado'].lower()}">{emoji} {op['resultado']}</span>
            </div>
            """
            
        # Função auxiliar para formatar porcentagem
        def pct(win, total):
            if total == 0: return "0%"
            return f"{int((win/total)*100)}%"

        html = f"""
        <!DOCTYPE html>
        <html lang="pt-BR">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Painel Clima do Mercado - {par}</title>
            <style>
                body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #0f172a; color: #e2e8f0; margin: 0; padding: 20px; }}
                .container {{ max-width: 600px; margin: 0 auto; background: #1e293b; border-radius: 12px; overflow: hidden; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
                .header {{ background: #334155; padding: 20px; text-align: center; border-bottom: 2px solid #475569; }}
                .header h1 {{ margin: 0; font-size: 1.2rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; }}
                .header h2 {{ margin: 5px 0 0; font-size: 1.8rem; color: #f8fafc; }}
                
                .status-banner {{ background: {cor_bg}; color: white; padding: 25px; text-align: center; }}
                .status-banner h3 {{ margin: 0; font-size: 2rem; font-weight: 800; }}
                .status-banner p {{ margin: 5px 0 0; font-size: 1.1rem; opacity: 0.9; }}
                
                .section {{ padding: 20px; border-bottom: 1px solid #334155; }}
                .section-title {{ font-size: 0.9rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 15px; font-weight: bold; display: flex; align-items: center; gap: 8px; }}
                
                .op-row {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #334155; font-size: 0.95rem; }}
                .op-row:last-child {{ border-bottom: none; }}
                .op-time {{ color: #cbd5e1; font-weight: bold; width: 50px; }}
                .op-dir {{ width: 50px; font-weight: bold; }}
                .op-est {{ color: #94a3b8; flex-grow: 1; text-align: center; }}
                .op-res {{ font-weight: bold; width: 100px; text-align: right; }}
                .op-res.win {{ color: #34d399; }}
                .op-res.loss {{ color: #f87171; }}
                
                .stat-grid {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; text-align: center; }}
                .stat-card {{ background: #0f172a; padding: 15px 10px; border-radius: 8px; border: 1px solid #334155; }}
                .stat-card h4 {{ margin: 0 0 10px; font-size: 0.85rem; color: #94a3b8; }}
                .stat-val {{ font-size: 1.4rem; font-weight: bold; margin-bottom: 5px; }}
                .stat-val.win {{ color: #34d399; }}
                .stat-val.loss {{ color: #f87171; }}
                .stat-pct {{ font-size: 0.8rem; color: #cbd5e1; background: #334155; padding: 2px 8px; border-radius: 10px; display: inline-block; }}
                
                .conclusion {{ background: #0f172a; padding: 20px; text-align: center; }}
                .conclusion h3 {{ color: #f8fafc; margin: 0 0 10px; }}
                .conclusion p {{ color: #94a3b8; margin: 0; font-size: 0.95rem; line-height: 1.5; }}
                
                .footer {{ text-align: center; padding: 15px; font-size: 0.75rem; color: #64748b; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>Catalogador de Clima do Mercado</h1>
                    <h2>{par} | M5</h2>
                </div>
                
                <div class="status-banner">
                    <h3>{status_texto}</h3>
                    <p>{conclusao}</p>
                </div>
                
                <div class="section">
                    <div class="section-title">📊 Últimas Operações Simuladas</div>
                    {linhas_historico}
                </div>
                
                <div class="section">
                    <div class="section-title">🧠 Taxa de Recuperação (Pós-Loss)</div>
                    <div class="stat-grid">
                        <div class="stat-card">
                            <h4>Após 1 LOSS</h4>
                            <div class="stat-val win">{stats['apos_1_loss']['win']}V / {stats['apos_1_loss']['loss']}D</div>
                            <span class="stat-pct">{pct(stats['apos_1_loss']['win'], stats['apos_1_loss']['total'])} Recuperação</span>
                        </div>
                        <div class="stat-card">
                            <h4>Após 2 LOSS</h4>
                            <div class="stat-val {'win' if stats['apos_2_loss']['win'] > stats['apos_2_loss']['loss'] else 'loss'}">{stats['apos_2_loss']['win']}V / {stats['apos_2_loss']['loss']}D</div>
                            <span class="stat-pct">{pct(stats['apos_2_loss']['win'], stats['apos_2_loss']['total'])} Recuperação</span>
                        </div>
                        <div class="stat-card">
                            <h4>Após 3 LOSS</h4>
                            <div class="stat-val {'win' if stats['apos_3_loss']['win'] > stats['apos_3_loss']['loss'] else 'loss'}">{stats['apos_3_loss']['win']}V / {stats['apos_3_loss']['loss']}D</div>
                            <span class="stat-pct">{pct(stats['apos_3_loss']['win'], stats['apos_3_loss']['total'])} Recuperação</span>
                        </div>
                    </div>
                </div>
                
                <div class="conclusion">
                    <h3>🎯 DECISÃO DO CATALOGADOR</h3>
                    <p>Baseado nas últimas 4 horas de mercado, o comportamento dos ativos e a assertividade das estratégias indicam que este é o momento ideal para <strong>{'LIGAR O BOT' if 'LIBERADO' in status_texto else 'AGUARDAR MELHOR CENÁRIO'}</strong>.</p>
                </div>
                
                <div class="footer">
                    Gerado automaticamente pelo Robô de Sinais em {datetime.datetime.now().strftime("%d/%m/%Y às %H:%M")}
                </div>
            </div>
        </body>
        </html>
        """
        return html

# ==========================================
# COMO USAR (Adicione isso ao final do seu script ou rode separadamente)
# ==========================================
def gerar_painel_clima():
    print("🔄 Conectando para gerar painel de clima do mercado...")
    # Reutiliza sua função de conexão
    api = conectar() 
    
    if not api:
        print("❌ Falha na conexão. Não foi possível gerar o painel.")
        return

    # Configurações do painel
    par_alvo = "EURUSD-OTC" # Pode colocar em um loop para todos os PARES
    estrategias_teste = ["MM", "PM", "M1", "M2", "MHI3", "FL", "TG", "P23", "REV", "C3", "V1", "TRI", "5VELA"]
    
    print(f"📊 Simulando últimas 4 horas (48 velas M5) para {par_alvo}...")
    
    catalogador = CatalogadorPainel(api, [par_alvo], estrategias_teste)
    historico = catalogador.simular_sessao(par_alvo, qtd_candles=48)
    
    if not historico:
        print("⚠️ Dados insuficientes para simulação.")
        return
        
    stats = catalogador.analisar_estatisticas(historico)
    decisao = catalogador.gerar_decisao(stats)
    
    print("🎨 Gerando painel HTML...")
    html_content = catalogador.gerar_html(par_alvo, historico, stats, decisao, decisao[2])
    
    # Salva o arquivo
    nome_arquivo = f"painel_clima_{par_alvo.replace('-', '_')}_{datetime.datetime.now().strftime('%H%M')}.html"
    with open(nome_arquivo, "w", encoding="utf-8") as f:
        f.write(html_content)
        
    print(f"✅ Painel gerado com sucesso!")
    print(f"📁 Salvo como: {os.path.abspath(nome_arquivo)}")
    
    # Abre automaticamente no navegador (opcional)
    try:
        webbrowser.open('file://' + os.path.realpath(nome_arquivo))
    except:
        pass

# Para testar agora, descomente a linha abaixo:
# gerar_painel_clima()
