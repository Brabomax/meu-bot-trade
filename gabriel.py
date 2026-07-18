import requests
from datetime import datetime
import sys, os, multiprocessing, time, threading, json, socket
import uuid
from flask import Flask, render_template_string, request, jsonify
import sqlite3
from iqoptionapi.stable_api import IQ_Option
import asyncio
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import re
from pycloudflared import try_cloudflare
import concurrent.futures

# ════════════════════════════════════════════════════════════════════════
# FUNÇÃO SALVA-VIDAS (TIMEOUT)
# ════════════════════════════════════════════════════════════════════════
def call_with_timeout(func, timeout, *args, **kwargs):
    """Executa uma função com timeout para evitar travamentos da API"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(func, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return "TIMEOUT"

# ════════════════════════════════════════════════════════════════════════
# BANCO DE DADOS SQLITE (COM TIMEOUT E ISOLAMENTO)
# ════════════════════════════════════════════════════════════════════════
DB_PATH = "shield_bots.db"

def init_database():
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('PRAGMA journal_mode=WAL;') # Permite múltiplas leituras/escritas simultâneas
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS active_bots (
            bot_id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            config TEXT NOT NULL,
            status TEXT DEFAULT 'running',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_heartbeat TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_database()

def salvar_bot_db(bot_id, email, config):
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO active_bots (bot_id, email, config, status, last_heartbeat)
        VALUES (?, ?, ?, 'running', CURRENT_TIMESTAMP)
    ''', (bot_id, email, json.dumps(config)))
    conn.commit()
    conn.close()

def atualizar_heartbeat(bot_id):
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE active_bots 
        SET last_heartbeat = CURRENT_TIMESTAMP 
        WHERE bot_id = ?
    ''', (bot_id,))
    conn.commit()
    conn.close()

def get_bot_status_db(bot_id):
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT bot_id, email, config, status, 
               julianday('now') - julianday(last_heartbeat) as days_since_heartbeat
        FROM active_bots 
        WHERE bot_id = ?
    ''', (bot_id,))
    result = cursor.fetchone()
    conn.close()
    if result:
        days_idle = result[4] if result[4] else 999
        is_alive = (days_idle * 24 * 60) < 2  # Menos de 2 minutos
        return {
            'exists': True,
            'status': result[3],
            'is_alive': is_alive,
            'config': json.loads(result[2]),
            'email': result[1]
        }
    return {'exists': False}

def remover_bot_db(bot_id):
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM active_bots WHERE bot_id = ?', (bot_id,))
    conn.commit()
    conn.close()

# ════════════════════════════════════════════════════════════════════════
# CONFIGURAÇÃO GIST (TRAVA RÍGIDA)
# ════════════════════════════════════════════════════════════════════════
URL_SISTEMA_GESTAO = "https://gist.githubusercontent.com/Brabomax/97ed147c10843a1c6f2b923df8243a65/raw/gistfile1.txt"

def verificar_acesso_remoto(email_digitado, proxies=None):
    """Verifica se o Gmail está autorizado e com data válida. Bloqueia se falhar."""
    try:
        r = requests.get(URL_SISTEMA_GESTAO, timeout=10, proxies=proxies)
        if r.status_code != 200:
            return False, "❌ Servidor de licenças offline. Tente mais tarde."
        
        for linha in r.text.splitlines():
            if '|' in linha:
                email_l, data_exp = linha.split('|')
                if email_digitado.strip().lower() == email_l.strip().lower():
                    try:
                        if datetime.now() < datetime.strptime(data_exp.strip(), '%Y-%m-%d'):
                            return True, "✅ Licença Ativa"
                        else:
                            return False, "❌ Licença deste Gmail expirada."
                    except Exception:
                        continue
        
        return False, "❌ Gmail não autorizado. Contate o administrador."
    except Exception:
        return False, "❌ Falha na conexão com o servidor de validação."

def verificar_membro_canal_telegram(api_id, api_hash, phone, session_str=""):
    """Verifica se o usuário autenticado faz parte do canal @botiqoption2"""
    try:
        async def check():
            sess = StringSession(session_str) if session_str else StringSession()
            client = TelegramClient(sess, int(api_id), api_hash)
            await client.connect()
            
            if not await client.is_user_authorized():
                await client.disconnect()
                return False, "❌ Sessão do Telegram expirada. Faça o login novamente."

            try:
                await client.get_permissions('botiqoption2', 'me')
                await client.disconnect()
                return True, "✅ Membro do canal @botiqoption2 verificado."
            except Exception:
                await client.disconnect()
                return False, "❌ ACESSO NEGADO: Você precisa entrar no canal t.me/botiqoption2 para usar o bot."

        loop = asyncio.new_event_loop()
        res = loop.run_until_complete(check())
        loop.close()
        return res
    except Exception as e:
        return False, f"❌ Erro na verificação do Telegram: {str(e)}"

# ════════════════════════════════════════════════════════════════════════
# FUNÇÕES PARA SALVAR/CARREGAR CREDENCIAIS (ISOLADO POR E-MAIL)
# ════════════════════════════════════════════════════════════════════════
def salvar_credenciais(email, senha):
    try:
        safe_email = email.replace('@', '_at_').replace('.', '_dot_').replace('/', '_')
        with open(f"cred_{safe_email}.txt", "w") as f:
            f.write(senha)
        return True
    except:
        return False

def carregar_senha(email):
    try:
        safe_email = email.replace('@', '_at_').replace('.', '_dot_').replace('/', '_')
        with open(f"cred_{safe_email}.txt", "r") as f:
            return f.read().strip()
    except:
        return None

# ════════════════════════════════════════════════════════════════════════
# PARSER DE RESULTADOS DO TELEGRAM
# ════════════════════════════════════════════════════════════════════════
def parsear_resultado_telegram(texto):
    try:
        texto_upper = texto.upper()
        if 'RESULTADO VIP' not in texto_upper and '#RESULTADO' not in texto_upper:
            return None
        tipo = None
        if '✅' in texto or '*WIN*' in texto_upper or 'WIN' in texto_upper:
            tipo = 'WIN'
        elif '❌' in texto or '*LOSS*' in texto_upper or 'LOSS' in texto_upper:
            tipo = 'LOSS'
        if not tipo:
            return None
        lucro = 0.0
        m = re.search(r'Lucro:\s*\$?(-?\d+(?:\.\d+)?)', texto, re.I)
        if m:
            lucro = float(m.group(1))
        return {'tipo': tipo, 'lucro': lucro}
    except Exception as e:
        print(f"Erro parsear resultado: {e}")
        return None

# ═══════════════════════════════════════════════════════════════════════
# PARSER DE SINAIS DO TELEGRAM
# ═══════════════════════════════════════════════════════════════════════
def parsear_sinal_telegram(texto):
    try:
        texto_upper = texto.upper()
        palavras_resultado = [
            'RESULTADO VIP', '#RESULTADO', '*WIN*', '*LOSS*',
            '✅ *WIN*', '❌ *LOSS*', '*LUCRO:', '*PREJUÍZO:', '*PREJUIZO:',
            'SEM GALE', 'GALE: G1', 'GALE: G2', 'GALE: G3', 'VENCEDOR', 'PERDEDOR'
        ]
        for palavra in palavras_resultado:
            if palavra in texto_upper:
                return None
        texto_limpo = texto.replace("*", "").replace("_", "")
        par = None
        horario = None
        duracao = 1
        direcao = None
        valor = 2.0

        m = re.search(r'Ativo:\s*([A-Z]+(?:-[A-Z]+)?)', texto_limpo, re.I)
        if m:
            par = m.group(1).strip()
            if not par.endswith('-OTC'):
                par = par + '-OTC'

        m = re.search(r'Timeframe:\s*M(\d+)', texto_limpo, re.I)
        if m:
            duracao = int(m.group(1))

        m = re.search(r'Direção:\s*(CALL|PUT)', texto_limpo, re.I)
        if m:
            direcao = m.group(1).lower()

        m = re.search(r'Entrada:\s*(\d{2}:\d{2}(?::\d{2})?)', texto_limpo)
        if m:
            horario = m.group(1).strip()

        m = re.search(r'Valor:\s*\$?(\d+(?:\.\d+)?)', texto_limpo)
        if m:
            valor = float(m.group(1))

        if par and horario and direcao:
            return {"par": par, "horario": horario, "duracao": duracao, "direcao": direcao, "valor": valor}

        linhas = [l.strip() for l in texto.strip().splitlines() if l.strip()]
        for linha in linhas:
            linha_limpa = linha.replace('*', '').replace('_', '').strip()
            if linha_limpa.startswith("📈") or linha_limpa.startswith("📉") or linha_limpa.startswith("📊"):
                par = linha_limpa[1:].strip().replace(" ", "")
                if '-' not in par and len(par) >= 6:
                    if len(par) == 6:
                        par = par[:3] + '/' + par[3:] + '-OTC'
                    else:
                        par = par + '-OTC'
            elif linha_limpa.startswith("⌛"):
                txt_dur = linha_limpa.replace("", "").lower().strip()
                nums = re.findall(r'\d+', txt_dur)
                if nums:
                    duracao = int(nums[0])
            elif "💰" in linha_limpa or "Valor:" in linha_limpa:
                nums = re.findall(r'\d+\.?\d*', linha_limpa)
                if nums:
                    valor = float(nums[0])
            if "PUT" in linha_limpa.upper() or "👇" in linha_limpa:
                direcao = "put"
            elif "CALL" in linha_limpa.upper() or "👆" in linha_limpa or "☝" in linha_limpa:
                direcao = "call"

        if not par:
            patterns = [r'[A-Z]{3,6}-OTC', r'[A-Z]{3,6}/[A-Z]{3,6}', r'[A-Z]{3,6}']
            for pattern in patterns:
                match = re.search(pattern, texto.upper())
                if match:
                    par = match.group()
                    if '/' in par:
                        par = par.replace('/', '')
                    if not par.endswith('-OTC'):
                        par = par + '-OTC'
                    break
        if not direcao:
            if "CALL" in texto.upper():
                direcao = "call"
            elif "PUT" in texto.upper():
                direcao = "put"

        if par and horario and direcao:
            return {"par": par, "horario": horario, "duracao": duracao, "direcao": direcao, "valor": valor or 2.00}
        return None
    except Exception as e:
        print(f"Erro parsear: {e}")
        return None

# ════════════════════════════════════════════════════════════════════════
# TELEGRAM LISTENER (COM RECONEXÃO AUTOMÁTICA)
# ════════════════════════════════════════════════════════════════════════
class TelegramListener(threading.Thread):
    def __init__(self, api_id, api_hash, phone, group_id, fila_sinais, fila_resultados, log_fn, session_str=""):
        super().__init__(daemon=True)
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.phone = phone
        self.group_id = int(group_id)
        self.fila_sinais = fila_sinais
        self.fila_resultados = fila_resultados
        self.log_fn = log_fn
        self.session_str = session_str
        self._stop_evt = threading.Event()
        self._client = None
        self._loop = None

    def log(self, msg):
        self.log_fn(f"📱 {msg}\n")

    def stop(self):
        self._stop_evt.set()
        if self._client and self._loop:
            try:
                asyncio.run_coroutine_threadsafe(self._client.disconnect(), self._loop)
            except:
                pass

    def run(self):
        import asyncio
        try:
            from telethon import TelegramClient, events
            from telethon.sessions import StringSession
        except ImportError:
            self.log("❌ Telethon não instalado. Execute: pip install telethon")
            return

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        session = StringSession(self.session_str) if self.session_str else StringSession()
        self._client = TelegramClient(session, self.api_id, self.api_hash, loop=self._loop)

        async def main():
            while not self._stop_evt.is_set():
                try:
                    if not self._client.is_connected():
                        await self._client.connect()
                    
                    if not await self._client.is_user_authorized():
                        self.log("❌ Sessão Telegram expirada. Precisa de novo código.")
                        break

                    self.log(f"✅ Conectado ao Telegram! Escutando grupo {self.group_id}")

                    @self._client.on(events.NewMessage)
                    async def handler(event):
                        try:
                            texto = event.raw_text or ""
                            if str(event.chat_id) != str(self.group_id): return
                            
                            resultado = parsear_resultado_telegram(texto)
                            if resultado:
                                self.log(f"📊 RESULTADO RECONHECIDO: {resultado['tipo']} (${resultado['lucro']:.2f})")
                                self.fila_resultados.append(resultado)
                                return
                                
                            sinal = parsear_sinal_telegram(texto)
                            if sinal:
                                self.log(f"✅ SINAL RECONHECIDO: {sinal}")
                                self.fila_sinais.append(sinal)
                            else:
                                self.log("❌ PARSER NÃO RECONHECEU A MENSAGEM")
                        except Exception as e:
                            self.log(f"❌ ERRO HANDLER: {e}")

                    self.log("🔄 Iniciando escuta de mensagens...")
                    await self._client.run_until_disconnected()
                    
                except Exception as e:
                    if not self._stop_evt.is_set():
                        self.log(f"⚠️ Erro no Telegram: {str(e)}. Reconectando em 15s...")
                        await asyncio.sleep(15)
                finally:
                    try:
                        if self._client.is_connected():
                            await self._client.disconnect()
                    except: pass

        try:
            self._loop.run_until_complete(main())
        except Exception as e:
            if not self._stop_evt.is_set():
                self.log(f"⚠️ Erro fatal: {str(e)}")
        finally:
            self.log("🔌 Desconectado")

# ════════════════════════════════════════════════════════════════════════
# IQ OPTION API (BLINDADA COM TIMEOUT - VERSÃO CONSERVADORA)
# ════════════════════════════════════════════════════════════════════════
class IQOptionAPI:
    def __init__(self, email, senha):
        self.email = email
        self.senha = senha
        self.api = None
        self.conectado = False
        self.tipo_conta = "PRACTICE"

    def connect(self, tipo_conta="PRACTICE"):
        try:
            if self.api:
                try:
                    self.api.close()
                except:
                    pass
                time.sleep(1)

            self.api = IQ_Option(self.email, self.senha)
            self.tipo_conta = tipo_conta

            check, reason = self.api.connect()
            if not check:
                self.conectado = False
                return False, f"Falha na conexão: {reason}"

            time.sleep(2)
            self.api.change_balance(tipo_conta)
            self.conectado = True

            saldo = self.api.get_balance()
            if saldo is None or saldo <= 0:
                self.conectado = False
                return False, "Saldo inválido"

            return True, f"Conectado | Saldo: ${saldo:.2f}"
        except Exception as e:
            self.conectado = False
            return False, f"Erro: {str(e)}"

    def reconnect(self):
        try:
            if self.api:
                try:
                    self.api.close()
                except:
                    pass
            time.sleep(2)
            self.api = IQ_Option(self.email, self.senha)
            check, reason = self.api.connect()
            if check:
                self.api.change_balance(self.tipo_conta)
                self.conectado = True
                return True, "Reconectado"
            self.conectado = False
            return False, reason
        except Exception as e:
            self.conectado = False
            return False, str(e)

    def check_connect(self):
        try:
            if self.api and self.conectado:
                res = call_with_timeout(self.api.check_connect, 5)
                return res != "TIMEOUT" and res
            return False
        except:
            return False

    def get_balance(self):
        try:
            if not self.check_connect():
                return None
            return self.api.get_balance()
        except Exception:
            return None

    def get_candles(self, par, timeframe, quantidade):
        """Versão conservadora: só reconecta se houver problema real"""
        try:
            if not self.check_connect():
                print(f"⚠️ Conexão instável detectada. Reconectando...")
                self.reconnect()
                time.sleep(2)
            
            candles = call_with_timeout(self.api.get_candles, 10, par, timeframe, quantidade, time.time())
            
            if candles == "TIMEOUT":
                print(f"⚠️ TIMEOUT em get_candles. Reconectando...")
                self.reconnect()
                return None
                
            # Validação: se retornar lista vazia ou incompleta, força reconexão
            if not candles or not isinstance(candles, list) or len(candles) < quantidade:
                print(f"⚠️ Dados de velas incompletos ({len(candles) if candles else 0}/{quantidade}). Reconectando...")
                self.reconnect()
                return None
                
            validated = []
            for c in candles:
                if all(k in c for k in ['open', 'close', 'max', 'min', 'from']):
                    try:
                        validated.append({
                            'open': float(c['open']), 'close': float(c['close']),
                            'high': float(c['max']), 'low': float(c['min']), 'time': c['from']
                        })
                    except: continue
            
            # Se após validar ainda faltar velas, reconecta
            if len(validated) < 20:
                print(f"⚠️ Velas validadas insuficientes ({len(validated)}). Reconectando...")
                self.reconnect()
                return None
                
            return validated
        except Exception as e:
            print(f"Erro crítico em get_candles: {e}. Reconectando...")
            try:
                self.reconnect()
            except:
                pass
            return None

    def buy(self, valor, par, direcao, duracao=1):
        if not self.check_connect():
            self.reconnect()
            time.sleep(1)

        saldo = self.get_balance()
        if saldo is None or saldo < valor:
            return False, f"Saldo insuficiente: ${saldo:.2f}"

        dir_iq = direcao.lower()
        print(f"🛒 COMPRANDO: {par} | {dir_iq} | ${valor:.2f} | {duracao}min")

        try:
            res = call_with_timeout(self.api.buy, 15, valor, par, dir_iq, duracao)
            if res == "TIMEOUT":
                self.reconnect()
                return False, "Timeout na compra"
            ok, oid = res
            if ok and oid:
                return True, oid
        except Exception as e:
            print("Erro buy padrão:", e)

        try:
            if hasattr(self.api, 'buy_digital_spot'):
                ok, oid = self.api.buy_digital_spot(par, valor, dir_iq, duracao)
                if ok and oid:
                    return True, oid
        except Exception as e:
            print("Erro buy_digital_spot:", e)

        try:
            if hasattr(self.api, 'api') and hasattr(self.api.api, 'buy'):
                ok, oid = self.api.api.buy(valor, par, dir_iq, duracao)
                if ok and oid:
                    return True, oid
        except Exception as e:
            print("Erro buy interno:", e)

        return False, "Nenhum método de compra funcionou"

    def check_win_v4(self, id_ordem, duracao_min=1):
        try:
            if not self.check_connect():
                return False, 0
            
            tempo_max_espera = (duracao_min * 60) + 20
            tempo_inicial = time.time()
            
            while time.time() - tempo_inicial < tempo_max_espera:
                time.sleep(5)
                try:
                    res = call_with_timeout(self.api.check_win_v3, 10, id_ordem)
                    if res != "TIMEOUT" and res is not None and res != 0:
                        return True, float(res)
                except Exception:
                    pass
            
            try:
                res = call_with_timeout(self.api.check_win_v3, 10, id_ordem)
                if res != "TIMEOUT" and res is not None:
                    return True, float(res)
            except: pass
                
            return True, -0.01
        except Exception as e:
            print(f"Erro check_win_v4: {e}")
            return False, 0

    def close(self):
        if self.api:
            try:
                self.api.close()
            except:
                pass
        self.conectado = False

# ════════════════════════════════════════════════════════════════════════
# MOTOR DE ESTRATÉGIAS
# ════════════════════════════════════════════════════════════════════════
class Motor:
    @staticmethod
    def analisar_sinal_unico(est, c):
        try:
            if not c or len(c) < 10:
                return None
            v = ["g" if x['close'] > x['open'] else "r" for x in c]
            if est == "MM":
                v5 = v[-5:]
                return "call" if v5.count("g") > v5.count("r") else "put"
            if est == "PM":
                p = [x['close'] for x in c]
                return "call" if p[-1] > (sum(p[-9:]) / 9) else "put"
            if est == "M1":
                v3 = v[-3:]
                return "put" if v3.count("g") > v3.count("r") else "call"
            if est == "FL":
                if v[-3:] == ["g"] * 3: return "call"
                if v[-3:] == ["r"] * 3: return "put"
                return None
            if est == "TG": return "call" if v[-1] == "g" else "put"
            if est == "M2":
                v5_m2 = v[-5:-2]
                return "put" if v5_m2.count("g") > v5_m2.count("r") else "call"
            if est == "P23": return "call" if v[-2] == "r" else "put"
            if est == "REV":
                if v[-4:] == ["g"] * 4: return "put"
                if v[-4:] == ["r"] * 4: return "call"
                return None
            if est == "EX4":
                u = v[-4:]
                return "put" if u == ["g"] * 4 else "call" if u == ["r"] * 4 else None
            if est == "C3":
                if len(v) >= 3 and v[-3] == v[-2]:
                    return "call" if v[-3] == "r" else "put" if v[-3] == "g" else None
                return None
            if est == "MHI3":
                v3 = v[-3:]
                return "call" if v3.count("r") > v3.count("g") else "put"
            if est == "V1":
                if len(v) >= 3:
                    return "call" if v[-3] == "g" and v[-2] == "g" else "put" if v[-3] == "r" and v[-2] == "r" else None
                return None
            if est == "TRI":
                if len(v) >= 3:
                    return "call" if v[-3:] == ["r", "r", "g"] else "put" if v[-3:] == ["g", "g", "r"] else None
                return None
            if est == "5VELA":
                if len(v) >= 4:
                    u = v[-4:]
                    if u == ["g"] * 4: return "put"
                    if u == ["r"] * 4: return "call"
                return None
            return None
        except:
            return None

class MotorIA:
    @staticmethod
    def calcular_filtros_pro(cand):
        try:
            if not cand or len(cand) < 20:
                return {"tendencia": "neutro", "sequencia_ok": True}
            p = [x['close'] for x in cand]
            sma20 = sum(p[-20:]) / 20
            tendencia = "call" if p[-1] > sma20 else "put"
            v = ["g" if x['close'] > x['open'] else "r" for x in cand]
            sequencia_ok = not (v[-4:] == ["g"] * 4 or v[-4:] == ["r"] * 4)
            return {"tendencia": tendencia, "sequencia_ok": sequencia_ok}
        except:
            return {"tendencia": "neutro", "sequencia_ok": True}

    @staticmethod
    def detectar_mercado(cand):
        try:
            if not cand or len(cand) < 20: return "lateral"
            p = [x['close'] for x in cand[-20:]]
            sma5 = sum(p[-5:]) / 5
            sma20 = sum(p) / 20
            diff = abs(sma5 - sma20) / sma20 if sma20 != 0 else 0
            return "tendencia" if diff >= 0.0015 else "lateral"
        except:
            return "lateral"

    @staticmethod
    def filtrar_volatilidade(cand):
        try:
            if not cand or len(cand) < 10: return True
            ranges = [x['high'] - x['low'] for x in cand[-10:]]
            avg = sum(ranges) / len(ranges)
            if avg == 0: return False
            ratio = ranges[-1] / avg
            return 0.3 <= ratio <= 3.0
        except:
            return True

    @staticmethod
    def detectar_velas_doidas(cand, max_pavios_permitidos=1, fator_pavio=2.5):
        try:
            if not cand or len(cand) < 5:
                return False
            
            velas_doidas = 0
            total_analisado = min(5, len(cand))
            
            for i in range(-total_analisado, 0):
                c = cand[i]
                corpo = abs(c['close'] - c['open'])
                
                if corpo == 0:
                    corpo = 0.0001
                
                pavio_superior = c['high'] - max(c['close'], c['open'])
                pavio_inferior = min(c['close'], c['open']) - c['low']
                
                pavio_max = max(pavio_superior, pavio_inferior)
                
                if pavio_max > (corpo * fator_pavio):
                    velas_doidas += 1
            
            return velas_doidas > max_pavios_permitidos
        except Exception as e:
            print(f"Erro detectar_velas_doidas: {e}")
            return False

    @staticmethod
    def catalogar_v36(api, par, estrategias_ativas):
        try:
            cand = api.get_candles(par, 60, 40)
            if not cand or len(cand) < 40: return {}
            rank = {}
            for e in estrategias_ativas:
                hits = 0
                for i in range(15, 39):
                    s = Motor.analisar_sinal_unico(e, cand[:i])
                    if s is None: continue
                    cor = "call" if cand[i]['close'] > cand[i]['open'] else "put"
                    if s == cor: hits += 1
                rank[e] = int((hits / 24) * 100)
            return rank
        except:
            return {}

# ═══════════════════════════════════════════════════════════════════════
# LOOP PRINCIPAL COM LOGS DETALHADOS E CONTADOR DE FALHAS (RESTAURADO)
# ════════════════════════════════════════════════════════════════════════
def loop_robo(sid, d, logs_dict):
    api = None
    vwin_counts = {}
    last_virtual_signal = {}
    sinais_processados = []
    ultimas_operacoes = {}
    prejuizo_acumulado = 0.0

    # 🔧 CONTADOR DE FALHAS DE CONEXÃO
    falhas_conexao = 0
    max_falhas = 5
    
    usar_ciclos = d.get('usar_ciclos', False)
    ciclos_config = d.get('ciclos', [])
    ciclo_atual = int(d.get('ciclo_inicial', 1))
    prejuizo_ciclo = 0.0
    ciclo_lock = threading.Lock()

    def get_valor_ciclo(gale_atual, v_ent_forcada):
        nonlocal ciclo_atual, prejuizo_ciclo
        if not usar_ciclos or not ciclos_config:
            return v_ent_forcada
        
        with ciclo_lock:
            idx = ciclo_atual - 1
            if idx < 0:
                idx = 0
            if idx >= len(ciclos_config):
                idx = len(ciclos_config) - 1
            
            ciclo = ciclos_config[idx]
            valores = [ciclo.get('entrada', v_ent_forcada)]
            if ciclo.get('g1', 0) > 0:
                valores.append(ciclo['g1'])
            if ciclo.get('g2', 0) > 0:
                valores.append(ciclo['g2'])
            if ciclo.get('g3', 0) > 0:
                valores.append(ciclo['g3'])
            
            if gale_atual < len(valores):
                return valores[gale_atual]
            else:
                return valores[-1] if valores else v_ent_forcada

    def avancar_ciclo():
        nonlocal ciclo_atual, prejuizo_ciclo
        with ciclo_lock:
            if ciclo_atual < len(ciclos_config):
                ciclo_atual += 1
                return True, f" CICLO {ciclo_atual-1} PERDIDO → CICLO {ciclo_atual}"
            else:
                ciclo_atual = 1
                prejuizo_ciclo = 0.0
                return False, "🔴 ÚLTIMO CICLO PERDIDO! Resetando para Ciclo 1"

    def voltar_ciclo_1():
        nonlocal ciclo_atual, prejuizo_ciclo
        with ciclo_lock:
            ciclo_anterior = ciclo_atual
            ciclo_atual = 1
            prejuizo_ciclo = 0.0
            return f"🔄 CICLO {ciclo_anterior} → CICLO 1 (WIN)"

    tg_timeframe = int(d.get('tg_timeframe', 5))
    auto_timeframe = int(d.get('auto_timeframe', 1))

    fila_telegram = []
    fila_resultados = []
    fila_lock = threading.Lock()
    resultados_lock = threading.Lock()
    tg_listener = None

    opera_apos_loss_ativo = d.get('opera_apos_loss', False)
    loss_count = 0
    win_count = 0
    wins_reais_bot = 0
    modo_operacao_liberado = False
    win_reset_target = int(d.get('win_reset_target', 3))

    def pode_operar(chave):
        agora = time.time()
        if chave in ultimas_operacoes:
            if agora - ultimas_operacoes[chave] < 60:
                return False
        ultimas_operacoes[chave] = agora
        return True

    def atualizar_log(m=None, **kwargs):
        temp = logs_dict.get(sid, {})
        if not temp:
            temp = {"msg": "", "wins": 0, "loss": 0, "lucro_sessao": 0.0,
                    "banca_real": 0.0, "status": "rodando", "banca_inicial": 0.0, "modo_ativo": "",
                    "loss_count": 0, "win_count": 0, "modo_operacao_liberado": False, "ciclo_atual": 1}
        if m:
            temp['msg'] = (temp.get('msg', '') + m)[-5000:]
        for key, val in kwargs.items():
            temp[key] = val
        temp['ciclo_atual'] = ciclo_atual
        logs_dict[sid] = temp

    def fila_pop():
        with fila_lock:
            return fila_telegram.pop(0) if fila_telegram else None

    def fila_pop_all():
        with fila_lock:
            sinais = list(fila_telegram)
            fila_telegram.clear()
            return sinais

    def resultados_pop_all():
        with resultados_lock:
            resultados = list(fila_resultados)
            fila_resultados.clear()
            return resultados

    def calcular_score_sinal(sinal, api_obj, estrategias_ativas):
        score = 0
        par = sinal.get('par', '')
        direcao = sinal.get('direcao', '')
        
        cand = api_obj.get_candles(par, 60, 40)
        if not cand or len(cand) < 20:
            return 0
        
        rank = MotorIA.catalogar_v36(api_obj, par, estrategias_ativas)
        if rank:
            best_rank = max(rank.values()) if rank else 0
            score += best_rank
        
        sinais_para_forca = []
        for e_forca in rank.keys():
            sf = Motor.analisar_sinal_unico(e_forca, cand)
            if sf:
                sinais_para_forca.append(sf)
        cv, pv = sinais_para_forca.count("call"), sinais_para_forca.count("put")
        forca = abs(cv - pv)
        score += forca * 10
        
        flts = MotorIA.calcular_filtros_pro(cand)
        
        if flts['tendencia'] == direcao:
            score += 30
        
        if flts['sequencia_ok']:
            score += 20
        
        if MotorIA.filtrar_volatilidade(cand):
            score += 15
        
        return score

    def gerenciar_operacao(api_obj, v_ent_forcada, par, direcao, sid_local, d_local, 
                           gale_atual=0, duracao=1, modo="auto"):
        nonlocal prejuizo_acumulado, ciclo_atual, prejuizo_ciclo, wins_reais_bot, win_count
        
        v_ent = get_valor_ciclo(gale_atual, v_ent_forcada)
        
        modo_str = "📱 TG" if modo == "telegram" else "🤖 AUTO"
        atualizar_log(f"{modo_str} [M{duracao}] {par} {direcao.upper()} ${v_ent:.2f} (Ciclo {ciclo_atual})\n")
        
        try:
            atualizar_log(f" EXECUTANDO COMPRA: {par} {direcao.upper()} ${v_ent:.2f} {duracao}min\n")
            ok, resultado = api_obj.buy(round(v_ent, 2), par, direcao, duracao=duracao)
            if not ok:
                atualizar_log(f"❌ BUY FALHOU: {resultado}\n")
                return False, 0, 0
            if not resultado or resultado == 0:
                atualizar_log(f"❌ ID DE ORDEM INVÁLIDO\n")
                return False, 0, 0

            atualizar_log(f"✅ Ordem enviada! ID: {resultado}\n")
            check, lucro = api_obj.check_win_v4(resultado, duracao_min=duracao)
            if check:
                b_at = api_obj.get_balance()
                if b_at is None:
                    b_at = logs_dict[sid_local].get('banca_real', 0)
                l_sessao = b_at - logs_dict[sid_local]['banca_inicial']

                if d_local.get('rec_continua', False) and lucro > 0 and prejuizo_acumulado > 0:
                    if lucro >= prejuizo_acumulado:
                        excesso = lucro - prejuizo_acumulado
                        prejuizo_acumulado = 0
                        atualizar_log(f"🎯 Recuperação Contínua COMPLETA! Lucro extra: ${excesso:.2f}\n")
                    else:
                        prejuizo_acumulado -= lucro
                        atualizar_log(f"🔄 Recuperação Contínua PARCIAL: abateu ${lucro:.2f}, restam ${prejuizo_acumulado:.2f}\n")

                if lucro > 0:
                    wins_reais_bot += 1
                    win_count = wins_reais_bot
                    
                    if usar_ciclos:
                        msg_ciclo = voltar_ciclo_1()
                        atualizar_log(f"{msg_ciclo}\n")
                    
                    atualizar_log(f"✅ WIN {par}! +${lucro:.2f} (Win real #{wins_reais_bot})\n",
                                  wins=logs_dict[sid_local].get('wins', 0) + 1,
                                  banca_real=b_at, lucro_sessao=l_sessao,
                                  ciclo_atual=ciclo_atual)
                    return True, v_ent, lucro
                else:
                    if d_local.get('rec_continua', False) or d_local.get('rec', False):
                        prejuizo_acumulado += v_ent
                        atualizar_log(f"📊 Prejuízo acumulado: ${prejuizo_acumulado:.2f}\n")

                    if d_local.get('use_gale') and gale_atual < int(d_local.get('max_gale', 1)):
                        fator = float(d_local.get('fator_gale', 100)) / 100
                        v_gale = round(v_ent * (1 + fator), 2)
                        atualizar_log(f"❌ LOSS {par} G{gale_atual} → Gale...\n")
                        return gerenciar_operacao(api_obj, v_gale, par, direcao,
                                                  sid_local, d_local, gale_atual + 1, duracao, modo)
                    else:
                        if usar_ciclos:
                            prejuizo_ciclo += v_ent
                            avancou, msg_ciclo = avancar_ciclo()
                            atualizar_log(f"{msg_ciclo}\n")
                            atualizar_log(f"💸 Prejuízo no ciclo: ${prejuizo_ciclo:.2f}\n")
                        
                        atualizar_log(f"❌ LOSS {par} G{gale_atual}\n",
                                      loss=logs_dict[sid_local].get('loss', 0) + 1,
                                      ciclo_atual=ciclo_atual)
                        return False, v_ent, lucro
            else:
                atualizar_log(f"⚠️ Timeout na verificação\n")
                return False, 0, 0
        except Exception as e:
            atualizar_log(f"⚠️ Erro Op: {str(e)}\n")
            return False, 0, 0

    def heartbeat_thread():
        contador = 0
        while True:
            time.sleep(25)
            contador += 1
            atualizar_log(f"💓 HEARTBEAT #{contador} - Wins Reais:{wins_reais_bot} Loss:{logs_dict[sid].get('loss', 0)} | Prej Acum: ${prejuizo_acumulado:.2f} | Ciclo: {ciclo_atual}\n")
            atualizar_heartbeat(sid)
            if not get_bot_status_db(sid).get('exists', False):
                break
    threading.Thread(target=heartbeat_thread, daemon=True).start()

    try:
        atualizar_log("🏁 Shield V37 IQ Option + Telegram Iniciado...\n")

        api = IQOptionAPI(d['user'], d['pass'])
        tipo_conta = d.get('tipo', 'PRACTICE')
        conectado, msg = api.connect(tipo_conta)
        if not conectado:
            atualizar_log(f"❌ ERRO LOGIN: {msg}\n", status="parado")
            return
        atualizar_log(f"✅ {msg}\n")

        b_ini = api.get_balance()
        if b_ini is None or b_ini <= 0:
            atualizar_log("❌ Falha ao obter saldo.\n", status="parado")
            return
        atualizar_log(f"💰 SALDO INICIAL: ${b_ini:.2f}\n", banca_real=b_ini, banca_inicial=b_ini)

        atualizar_log(f"⏱️ Timeframes: Telegram M{tg_timeframe} | Estratégias M{auto_timeframe}\n")
        
        if usar_ciclos and ciclos_config:
            atualizar_log(f"🔄 GERENCIAMENTO POR CICLOS ATIVADO ({len(ciclos_config)} ciclos)\n")
            for i, ciclo in enumerate(ciclos_config, 1):
                valores = f"Ent:${ciclo.get('entrada',0)} G1:${ciclo.get('g1',0)} G2:${ciclo.get('g2',0)} G3:${ciclo.get('g3',0)}"
                atualizar_log(f"   Ciclo {i}: {valores}\n")
            atualizar_log(f"🎯 Ciclo inicial: {ciclo_atual}\n")

        if d.get('filtro_velas_doidas'):
            fator_pavio = float(d.get('fator_pavio', 2.5))
            max_pavios = int(d.get('max_pavios_permitidos', 1))
            atualizar_log(f"🛑 FILTRO VELAS DOIDAS ATIVADO | Fator: {fator_pavio}x | Máx: {max_pavios}\n")

        modo_telegram = d.get('modo_telegram', False)
        if modo_telegram:
            try:
                atualizar_log("📱 Iniciando conexão com Telegram...\n")
                tg_listener = TelegramListener(
                    api_id=d['tg_api_id'],
                    api_hash=d['tg_api_hash'],
                    phone=d['tg_phone'],
                    group_id=d['tg_group_id'],
                    fila_sinais=fila_telegram,
                    fila_resultados=fila_resultados,
                    log_fn=lambda m: atualizar_log(f"📱 {m}\n"),
                    session_str=d.get('tg_session', '')
                )
                tg_listener.start()
                atualizar_log("✅ Telegram conectado!\n", modo_ativo=" Telegram")
            except Exception as e:
                atualizar_log(f"❌ Erro Telegram: {e}\n")

        estrategias_ativas = d.get('estrategias', ["MM"])
        min_rank_filtro = int(d.get('min_rank', 50))
        last_min = -1
        sinais_tg_executados = set()
        sinais_por_horario_executados = {}
        modo_catalogo = d.get('modo_catalogo', False)

        lista_pares_iq = ["EURUSD-OTC", "GBPUSD-OTC", "EURGBP-OTC"]
        if d.get('par'):
            pares_personalizados = [p.strip().upper() for p in d['par'].split(',')]
            if pares_personalizados:
                lista_pares_iq = pares_personalizados
        atualizar_log(f" PARES: {', '.join(lista_pares_iq)}\n")

        if opera_apos_loss_ativo:
            loss_target = int(d.get('loss_target', 2))
            atualizar_log(f"🎯 MODO 'OPERA APÓS LOSS' ATIVADO - Aguardando {loss_target} LOSS seguidos\n")
            atualizar_log(f"🔄 Reset após {win_reset_target} WINS REAIS\n")
            atualizar_log(loss_count=0, win_count=0, modo_operacao_liberado=False)

        while True:
            try:
                time.sleep(1)
                now = datetime.now()
                hora_atual = now.strftime("%H:%M")

                # ═══════════════════════════════════════════════════════════
                # LIMPEZA DE MEMÓRIA A CADA 1 HORA (Evita travamento por acúmulo)
                # ═══════════════════════════════════════════════════════════
                if now.minute == 0 and now.second == 0:
                    tempo_limite = time.time() - 7200  # 2 horas em segundos
                    
                    # Limpa dicionário de operações antigas
                    chaves_para_remover = [k for k, v in ultimas_operacoes.items() if v < tempo_limite]
                    for k in chaves_para_remover:
                        del ultimas_operacoes[k]
                    
                    # Limpa set de sinais executados se ficar muito grande
                    if len(sinais_tg_executados) > 500:
                        sinais_tg_executados.clear()
                        atualizar_log("🧹 Memória de sinais limpa para evitar lentidão.\n")

                # 🔧 VERIFICAÇÃO DE CONEXÃO COM CONTADOR DE FALHAS
                if not api.check_connect():
                    falhas_conexao += 1
                    atualizar_log(f"🔄 Reconectando IQ... (Falha #{falhas_conexao}/{max_falhas})\n")
                    
                    if falhas_conexao >= max_falhas:
                        atualizar_log(f"❌ MUITAS FALHAS DE CONEXÃO! Aguardando 30s...\n")
                        time.sleep(30)
                        falhas_conexao = 0
                    
                    api.reconnect()
                    time.sleep(3)
                    
                    if api.check_connect():
                        atualizar_log(f"✅ Reconectado com sucesso!\n")
                        falhas_conexao = 0
                    else:
                        atualizar_log(f"⚠️ Ainda sem conexão. Tentando novamente...\n")
                    
                    continue
                else:
                    if falhas_conexao > 0:
                        atualizar_log(f"✅ Conexão estabilizada\n")
                        falhas_conexao = 0

                if now.second % 5 == 0:
                    b_atual = api.get_balance()
                    if b_atual is not None and b_atual > 0:
                        lucro_at = b_atual - b_ini
                        atualizar_log(banca_real=b_atual, lucro_sessao=lucro_at, ciclo_atual=ciclo_atual)
                        if lucro_at >= float(d['sw']) or lucro_at <= -float(d['sl']):
                            atualizar_log(f" STOP ALCANÇADO: ${lucro_at:.2f}\n", status="finalizado")
                            if opera_apos_loss_ativo:
                                loss_count = 0
                                win_count = 0
                                wins_reais_bot = 0
                                modo_operacao_liberado = False
                                atualizar_log(f"🔄 Resetando contadores (aguardando {loss_target} LOSS novamente)\n")
                                atualizar_log(loss_count=0, win_count=0, modo_operacao_liberado=False)
                                continue
                            else:
                                break

                if opera_apos_loss_ativo and modo_telegram:
                    resultados = resultados_pop_all()
                    if resultados:
                        for resultado in resultados:
                            if resultado['tipo'] == 'LOSS':
                                loss_count += 1
                                win_count = 0
                                wins_reais_bot = 0
                                atualizar_log(f"❌ LOSS detectado! Contador: {loss_count}/{loss_target}\n")
                                atualizar_log(loss_count=loss_count, win_count=0)
                                
                                if loss_count >= loss_target and not modo_operacao_liberado:
                                    modo_operacao_liberado = True
                                    win_count = 0
                                    wins_reais_bot = 0
                                    atualizar_log(f"🎯 META ATINGIDA! {loss_count} LOSS seguidos - LIBERANDO OPERAÇÕES\n")
                                    atualizar_log(f"📊 Agora preciso de {win_reset_target} WINS REAIS para voltar a esperar LOSS\n")
                                    atualizar_log(modo_operacao_liberado=True, win_count=0)
                                    
                            elif resultado['tipo'] == 'WIN':
                                atualizar_log(f"📊 Telegram reportou WIN (log apenas, não conta)\n")
                                
                                if modo_operacao_liberado and wins_reais_bot >= win_reset_target:
                                    modo_operacao_liberado = False
                                    loss_count = 0
                                    wins_reais_bot = 0
                                    win_count = 0
                                    atualizar_log(f"🔄 {win_reset_target} WINS REAIS ATINGIDOS! Voltando a esperar {loss_target} LOSS...\n")
                                    atualizar_log(modo_operacao_liberado=False, loss_count=0, win_count=0)
                            else:
                                if loss_count > 0:
                                    atualizar_log(f"✅ WIN detectado - Resetando contador de LOSS ({loss_count} → 0)\n")
                                    loss_count = 0
                                    atualizar_log(loss_count=0)

                if modo_catalogo and now.second == 0 and now.minute != last_min:
                    last_min = now.minute
                    atualizar_log(f"\n📊 ═══ CATALOGADOR v36 ═══\n")
                    for par_cat in lista_pares_iq:
                        rank = MotorIA.catalogar_v36(api, par_cat, estrategias_ativas)
                        if rank:
                            atualizar_log(f"\n📈 {par_cat}:\n")
                            for est, perc in sorted(rank.items(), key=lambda x: x[1], reverse=True):
                                bar = "█" * (perc // 10) + "░" * (10 - perc // 10)
                                atualizar_log(f"  {est:5s}: {bar} {perc:3d}%\n")
                    atualizar_log(f"═══════════════════════\n\n")

                # 🔧 PROCESSAMENTO DE SINAIS COM LOGS DETALHADOS
                if modo_telegram:
                    sinais_coletados = fila_pop_all()
                    
                    if sinais_coletados:
                        atualizar_log(f"📨 {len(sinais_coletados)} sinal(is) recebido(s) [M{tg_timeframe}]\n")
                        
                        if opera_apos_loss_ativo and not modo_operacao_liberado:
                            atualizar_log(f"⏸️ Aguardando {loss_target - loss_count} LOSS seguidos para liberar operações...\n")
                            continue
                        
                        sinais_por_horario = {}
                        for sinal in sinais_coletados:
                            horario = sinal.get('horario', '')
                            if horario not in sinais_por_horario:
                                sinais_por_horario[horario] = []
                            sinais_por_horario[horario].append(sinal)
                        
                        for horario, sinais in sinais_por_horario.items():
                            sinais_ja_executados = sinais_por_horario_executados.get(horario, 0)
                            max_sinais_horario = int(d.get('max_sinais_horario', 999))
                            
                            if sinais_ja_executados >= max_sinais_horario:
                                atualizar_log(f"⏰ Limite de {max_sinais_horario} sinal(is) para {horario} já atingido\n")
                                continue
                            
                            sinais_validos = []
                            for idx_sinal, sinal in enumerate(sinais, 1):
                                par_tg = sinal.get('par', 'EURUSD-OTC')
                                dir_tg = sinal.get('direcao', 'call')
                                valor_tg = sinal.get('valor', float(d.get('ent', 2.00)))
                                duracao_tg = sinal.get('duracao', tg_timeframe)
                                
                                atualizar_log(f"🔍 Analisando sinal {idx_sinal}/{len(sinais)}: {par_tg} {dir_tg.upper()} {horario}\n")
                                
                                chave_tg = f"{par_tg}_{horario}_{dir_tg}"
                                if chave_tg in sinais_tg_executados:
                                    atualizar_log(f"⏭️ {par_tg} {horario} já executado anteriormente\n")
                                    continue
                                
                                atualizar_log(f"   📊 Buscando candles para {par_tg}...\n")
                                cand_tg = api.get_candles(par_tg, 60, 40)
                                if not cand_tg:
                                    atualizar_log(f"   ❌ FALHA: Não foi possível obter candles para {par_tg}\n")
                                    continue
                                atualizar_log(f"   ✅ Candles obtidos: {len(cand_tg)} velas\n")
                                
                                flts_tg = MotorIA.calcular_filtros_pro(cand_tg)
                                
                                if d.get("filtro_confluencia") and flts_tg['tendencia'] != dir_tg:
                                    atualizar_log(f"   🚫 REJEITADO: Contra tendência ({flts_tg['tendencia']} vs {dir_tg})\n")
                                    continue
                                
                                if d.get("filtro_antiloss") and not flts_tg['sequencia_ok']:
                                    atualizar_log(f"   🛑 REJEITADO: Anti-Loss (sequência de 4 velas iguais)\n")
                                    continue
                                
                                if d.get("filtro_volatilidade") and not MotorIA.filtrar_volatilidade(cand_tg):
                                    atualizar_log(f"   ⚡ REJEITADO: Volatilidade fora do padrão\n")
                                    continue
                                
                                if d.get("filtro_velas_doidas"):
                                    fator_pavio = float(d.get("fator_pavio", 2.5))
                                    max_pavios = int(d.get("max_pavios_permitidos", 1))
                                    
                                    if MotorIA.detectar_velas_doidas(cand_tg, max_pavios, fator_pavio):
                                        atualizar_log(f"   🛑 REJEITADO: Velas doidas (pavios excessivos)\n")
                                        continue
                                
                                if d.get("modo_inteligente"):
                                    tipo_mercado = MotorIA.detectar_mercado(cand_tg)
                                    if tipo_mercado == "lateral":
                                        sinal_5v = Motor.analisar_sinal_unico("5VELA", cand_tg)
                                        if not sinal_5v or sinal_5v != dir_tg:
                                            atualizar_log(f"   🧠 REJEITADO: Mercado lateral\n")
                                            continue
                                
                                if d.get("usar_5vela"):
                                    sinal_5v = Motor.analisar_sinal_unico("5VELA", cand_tg)
                                    if not sinal_5v or sinal_5v != dir_tg:
                                        atualizar_log(f"   🎯 REJEITADO: 5ª Vela não confirma\n")
                                        continue
                                
                                rank_tg = None
                                best_rank = 0
                                
                                if not d.get('tg_sem_ranking', False):
                                    atualizar_log(f"   📈 Calculando ranking...\n")
                                    rank_tg = MotorIA.catalogar_v36(api, par_tg, estrategias_ativas)
                                    if rank_tg:
                                        best_rank = max(rank_tg.values()) if rank_tg else 0
                                        if best_rank < min_rank_filtro:
                                            atualizar_log(f"   📉 REJEITADO: Ranking {best_rank}% < {min_rank_filtro}%\n")
                                            continue
                                        atualizar_log(f"   ✅ Ranking: {best_rank}%\n")
                                    else:
                                        atualizar_log(f"   ⚠️ REJEITADO: Não foi possível calcular ranking\n")
                                        continue
                                else:
                                    atualizar_log(f"   📱 Ignorando filtro de ranking\n")
                                    best_rank = 100
                                
                                atualizar_log(f"   🎯 Calculando score...\n")
                                score = calcular_score_sinal(sinal, api, estrategias_ativas)
                                atualizar_log(f"   ✅ Score: {score}\n")
                                
                                sinais_validos.append({
                                    'sinal': sinal,
                                    'score': score,
                                    'cand': cand_tg,
                                    'rank': best_rank
                                })
                                atualizar_log(f"   ✅ Sinal APROVADO para execução\n")
                            
                            sinais_validos.sort(key=lambda x: x['score'], reverse=True)
                            sinais_para_executar = sinais_validos[:max_sinais_horario - sinais_ja_executados]
                            
                            if not sinais_para_executar:
                                atualizar_log(f"⚠️ Nenhum sinal válido para {horario} (todos rejeitados)\n")
                                continue
                            
                            atualizar_log(f"🎯 {len(sinais_para_executar)} sinal(is) selecionado(s) para {horario}\n")
                            
                            for sinal_info in sinais_para_executar:
                                sinal = sinal_info['sinal']
                                par_tg = sinal.get('par', 'EURUSD-OTC')
                                dir_tg = sinal.get('direcao', 'call')
                                horario_tg = sinal.get('horario')
                                valor_tg = sinal.get('valor', float(d.get('ent', 2.00)))
                                duracao_tg = sinal.get('duracao', tg_timeframe)
                                
                                try:
                                    if ':' in horario_tg:
                                        partes = horario_tg.split(':')
                                        if len(partes) == 3:
                                            hora, minuto, segundo = map(int, partes)
                                        else:
                                            hora, minuto = map(int, partes)
                                            segundo = 0
                                        hora_alvo = datetime.now().replace(hour=hora, minute=minuto, second=segundo, microsecond=0)
                                    else:
                                        hora_alvo = datetime.now()
                                    
                                    diff = (hora_alvo - datetime.now()).total_seconds()
                                    if diff < -30:  # Aumentado para 30 segundos de tolerância
                                        atualizar_log(f"⏰ Sinal muito atrasado {horario_tg} ({diff:.0f}s) - pulando\n")
                                        continue
                                    elif diff > 0:
                                        atualizar_log(f"⏳ Aguardando {horario_tg} ({diff:.0f}s)\n")
                                        time.sleep(diff)
                                except Exception as e:
                                    atualizar_log(f"⚠️ Erro ao processar horário: {e}\n")
                                    continue
                                
                                v_ent_tg = round(valor_tg, 2)
                                if d.get('rec_continua') and prejuizo_acumulado > 0:
                                    percent_rec = float(d.get('rec_percent', 100)) / 100
                                    v_ent_tg += prejuizo_acumulado * percent_rec
                                    v_ent_tg *= 1.2
                                v_ent_tg = round(v_ent_tg, 2)
                                
                                chave_tg = f"{par_tg}_{horario_tg}_{dir_tg}"
                                if not pode_operar(chave_tg):
                                    atualizar_log(f"⏭️ {par_tg} bloqueado por pode_operar (operou há menos de 60s)\n")
                                    continue
                                
                                sinais_tg_executados.add(chave_tg)
                                
                                atualizar_log(f"📨 EXECUTANDO: {par_tg} {dir_tg.upper()} {horario_tg} | Score: {sinal_info['score']} | Rank: {sinal_info['rank']}%\n")
                                
                                ok, v_ent_final, lucro = gerenciar_operacao(api, v_ent_tg, par_tg, dir_tg, sid, d, 
                                                 duracao=duracao_tg, modo="telegram")
                                
                                if not ok:
                                    sinais_tg_executados.discard(chave_tg)
                                    atualizar_log(f"🔄 Sinal removido da lista de executados (permitindo retentativa)\n")
                                
                                sinais_por_horario_executados[horario] = sinais_por_horario_executados.get(horario, 0) + 1

                if d.get('modo_lista') and d.get('lista_sinais'):
                    if now.second == 0 and hora_atual not in sinais_processados:
                        if opera_apos_loss_ativo and not modo_operacao_liberado:
                            continue
                        
                        linhas = d['lista_sinais'].split('\n')
                        for linha in linhas:
                            if ';' in linha or ',' in linha:
                                partes = linha.replace(';', ',').split(',')
                                if len(partes) >= 3 and partes[0].strip() == hora_atual:
                                    p_lista, d_lista = partes[1].strip(), partes[2].strip().lower()
                                    chave_lista = f"{p_lista}_{hora_atual}_{d_lista}"
                                    if not pode_operar(chave_lista): continue
                                    cand_lista = api.get_candles(p_lista, 60, 40)
                                    if cand_lista:
                                        flts_lista = MotorIA.calcular_filtros_pro(cand_lista)
                                        if d.get("filtro_confluencia") and flts_lista['tendencia'] != d_lista: continue
                                        if d.get("filtro_antiloss") and not flts_lista['sequencia_ok']: continue
                                    v_ent_lista = round(float(d['ent']), 2)
                                    if d.get('rec_continua') and prejuizo_acumulado > 0:
                                        percent_rec = float(d.get('rec_percent', 100)) / 100
                                        v_ent_lista += prejuizo_acumulado * percent_rec
                                        v_ent_lista *= 1.2
                                    v_ent_lista = round(v_ent_lista, 2)
                                    atualizar_log(f"📅 Lista: {p_lista} {hora_atual}\n")
                                    gerenciar_operacao(api, v_ent_lista, p_lista, d_lista, sid, d, modo="lista")
                        sinais_processados.append(hora_atual)

                if not d.get('modo_lista') and not modo_telegram and now.second == 57 and now.minute != last_min:
                    if opera_apos_loss_ativo and not modo_operacao_liberado:
                        continue
                    
                    last_min = now.minute
                    candidatos = []
                    for p_at in lista_pares_iq:
                        usar_analise = (d.get('modo_inteligente') or d.get('usar_5vela') or d.get('filtro_volatilidade'))
                        cand_pre = None
                        if usar_analise:
                            cand_pre = api.get_candles(p_at, auto_timeframe, 40)
                            if not cand_pre: continue
                        if (d.get('filtro_volatilidade') or d.get('modo_inteligente')) and cand_pre:
                            if not MotorIA.filtrar_volatilidade(cand_pre): continue
                        if d.get('modo_inteligente') and cand_pre:
                            tipo_mercado = MotorIA.detectar_mercado(cand_pre)
                            if tipo_mercado == "lateral":
                                sinal_5v = Motor.analisar_sinal_unico("5VELA", cand_pre)
                                if sinal_5v:
                                    candidatos.append({'par': p_at, 'est': '5VELA', 'perc': 80,
                                                       'sinal': sinal_5v, 'cand': cand_pre, 'forca': 4})
                                continue

                        rank = MotorIA.catalogar_v36(api, p_at, estrategias_ativas)
                        if not rank: continue
                        rank_filtrado = {k: v for k, v in rank.items() if v >= min_rank_filtro}
                        if not rank_filtrado: continue
                        cand_forca = cand_pre if cand_pre else api.get_candles(p_at, auto_timeframe, 40)
                        if not cand_forca: continue
                        sinais_para_forca = []
                        for e_forca in rank_filtrado.keys():
                            sf = Motor.analisar_sinal_unico(e_forca, cand_forca)
                            if sf: sinais_para_forca.append(sf)
                        cv, pv = sinais_para_forca.count("call"), sinais_para_forca.count("put")
                        forca_calc = abs(cv - pv)
                        best_est = max(rank_filtrado, key=rank_filtrado.get)
                        sinal = Motor.analisar_sinal_unico(best_est, cand_forca)
                        if sinal:
                            candidatos.append({'par': p_at, 'est': best_est, 'perc': rank_filtrado[best_est],
                                               'sinal': sinal, 'cand': cand_forca, 'forca': forca_calc})

                    if candidatos:
                        candidatos.sort(key=lambda x: x['perc'], reverse=True)
                        melhor = candidatos[0]
                        chave_auto = f"{melhor['par']}_{hora_atual}_{melhor['sinal']}"
                        if not pode_operar(chave_auto): continue
                        if d.get("filtro_forca") and melhor['forca'] < int(d.get("min_forca", 3)): continue
                        flts = MotorIA.calcular_filtros_pro(melhor['cand'])
                        if d.get("filtro_confluencia") and flts['tendencia'] != melhor['sinal']: continue
                        if d.get("filtro_antiloss") and not flts['sequencia_ok']: continue
                        v_ent = round(float(d['ent']), 2)
                        if d.get('rec_continua') and prejuizo_acumulado > 0:
                            percent_rec = float(d.get('rec_percent', 100)) / 100
                            v_ent += prejuizo_acumulado * percent_rec
                            v_ent *= 1.2
                        v_ent = round(v_ent, 2)
                        atualizar_log(f"💰 {melhor['par']} ({melhor['est']}): {melhor['sinal'].upper()} | ${v_ent:.2f}\n")
                        
                        gerenciar_operacao(api, v_ent, melhor['par'], melhor['sinal'], sid, d,
                                         duracao=auto_timeframe, modo="auto")

            except Exception as e:
                atualizar_log(f"⚠️ Erro no loop: {str(e)}\n")
                time.sleep(2)
    except Exception as e:
        atualizar_log(f"❌ ERRO CRÍTICO: {str(e)}\n", status="parado")
    finally:
        try:
            if tg_listener: tg_listener.stop()
            if api: api.close()
            remover_bot_db(sid)
            if sid in logs_dict: del logs_dict[sid]
        except:
            pass

# ════════════════════════════════════════════════════════════════════════
# FLASK e rotas (MULTI-USUÁRIO SEGURO)
# ════════════════════════════════════════════════════════════════════════
app = Flask(__name__)

logs_web = {} 
processos = {}
tg_sessions_storage = {}  # Agora keyed por SID para isolamento total

# [O HTML_SISTEMA continua exatamente igual ao original - muito longo para incluir aqui]
# Copie o HTML do seu código original e cole aqui

@app.route('/tg_listar_grupos', methods=['POST'])
def tg_listar_grupos():
    data = request.json
    sid = data.get('sid', 'default')  # Isolamento por sessão do usuário
    api_id = data.get('api_id')
    api_hash = data.get('api_hash')
    phone = data.get('phone')
    session = data.get('session', '')
    if not api_id or not api_hash or not phone:
        return jsonify({"ok": False, "erro": "Dados incompletos"})
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        return jsonify({"ok": False, "erro": "Telethon não instalado"})

    async def _run():
        sess = StringSession(session) if session else StringSession()
        client = TelegramClient(sess, int(api_id), api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                result = await client.send_code_request(phone)
                tg_sessions_storage[sid] = {  # Usa SID como chave
                    "session": client.session.save(),
                    "phone_code_hash": result.phone_code_hash
                }
                await client.disconnect()
                return {"ok": False, "precisa_codigo": True}
            grupos = []
            async for dialog in client.iter_dialogs():
                if dialog.is_group or dialog.is_channel:
                    grupos.append({"id": str(dialog.id), "nome": dialog.name})
            sess_str = client.session.save()
            await client.disconnect()
            return {"ok": True, "grupos": grupos, "session": sess_str}
        except Exception as e:
            await client.disconnect()
            return {"ok": False, "erro": str(e)}

    loop = asyncio.new_event_loop()
    res = loop.run_until_complete(_run())
    loop.close()
    return jsonify(res)

@app.route('/tg_confirmar_codigo', methods=['POST'])
def tg_confirmar_codigo():
    data = request.json
    sid = data.get('sid', 'default')  # Isolamento por sessão do usuário
    api_id = data.get('api_id')
    api_hash = data.get('api_hash')
    phone = data.get('phone')
    code = data.get('code')
    session = data.get('session', '')
    dados_salvos = tg_sessions_storage.get(sid, {})  # Usa SID como chave
    phone_code_hash = dados_salvos.get("phone_code_hash", "")
    session_str = session or dados_salvos.get("session", "")
    if not phone_code_hash:
        return jsonify({"ok": False, "erro": "Nenhum código pendente."})

    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        return jsonify({"ok": False, "erro": "Telethon não instalado"})

    async def _run():
        sess = StringSession(session_str) if session_str else StringSession()
        client = TelegramClient(sess, int(api_id), api_hash)
        try:
            await client.connect()
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            grupos = []
            async for dialog in client.iter_dialogs():
                if dialog.is_group or dialog.is_channel:
                    grupos.append({"id": str(dialog.id), "nome": dialog.name})
            sess_str = client.session.save()
            await client.disconnect()
            if sid in tg_sessions_storage:
                del tg_sessions_storage[sid]
            return {"ok": True, "grupos": grupos, "session": sess_str}
        except Exception as e:
            try: await client.disconnect()
            except: pass
            return {"ok": False, "erro": str(e)}

    loop = asyncio.new_event_loop()
    res = loop.run_until_complete(_run())
    loop.close()
    return jsonify(res)

@app.route('/')
def index():
    return render_template_string(HTML_SISTEMA)

@app.route('/status/<sid>')
def get_status(sid):
    db_status = get_bot_status_db(sid)
    if db_status.get('exists') and not db_status.get('is_alive'):
        if sid in processos:
            try:
                processos[sid].terminate()
                del processos[sid]
            except: pass
        return jsonify({
            "msg": "\n❌ BOT TRAVOU! (Sem heartbeat por 2 min). Reinicie o bot.\n", 
            "status": "travado"
        })

    if sid in logs_web:
        res = logs_web[sid].copy()
        temp = logs_web[sid]
        temp['msg'] = ""
        logs_web[sid] = temp
        return jsonify(res)
        
    return jsonify({"msg": ""})

@app.route('/check_bot/<sid>')
def check_bot(sid):
    return jsonify(get_bot_status_db(sid))

@app.route('/reconectar/<sid>', methods=['POST'])
def reconectar(sid):
    bot_info = get_bot_status_db(sid)
    if not bot_info['exists']:
        return jsonify({"ok": False, "erro": "Nenhum bot encontrado"})
    if not bot_info['is_alive']:
        return jsonify({"ok": False, "erro": "Bot offline"})
    if sid not in processos:
        return jsonify({"ok": False, "erro": "Processo não encontrado"})
    return jsonify({"ok": True})

@app.route('/ligar', methods=['POST'])
def ligar():
    d = request.json
    sid = d.get('id')

    # CORREÇÃO MULTI-USUÁRIO: Verifica apenas se ESTE usuário já tem um bot rodando
    if sid in processos and processos[sid].is_alive():
        return jsonify({"erro": "⚠️ Este usuário já tem um bot rodando!"})

    email = d.get('user', '').strip()
    senha = d.get('pass', '').strip()

    if not senha:
        senha = carregar_senha(email)
        if not senha:
            return jsonify({"erro": "❌ Nenhuma senha encontrada. Informe a senha da IQ Option."})
        d['pass'] = senha
    else:
        salvar_credenciais(email, senha)

    # TRAVA 1: Verifica se o Gmail está na lista do Gist
    ok_gmail, msg_gmail = verificar_acesso_remoto(email)
    if not ok_gmail:
        return jsonify({"erro": msg_gmail})

    # TRAVA 2: Verifica se está no canal do Telegram
    tg_api_id = d.get('tg_api_id', '').strip()
    tg_api_hash = d.get('tg_api_hash', '').strip()
    tg_phone = d.get('tg_phone', '').strip()
    tg_session = d.get('tg_session', '')

    if tg_api_id and tg_api_hash and tg_phone:
        ok_tg, msg_tg = verificar_membro_canal_telegram(tg_api_id, tg_api_hash, tg_phone, tg_session)
        if not ok_tg:
            return jsonify({"erro": msg_tg})

    # Se passou por todas as travas, salva e inicia o bot ISOLADO
    salvar_bot_db(sid, email, d)

    p = multiprocessing.Process(target=loop_robo, args=(sid, d, logs_web))
    p.daemon = False
    p.start()
    processos[sid] = p  # Armazena pelo SID, permitindo múltiplos usuários

    return jsonify({"s": "ok", "msg": "✅ Bot iniciado com sucesso!"})

@app.route('/parar', methods=['POST'])
def parar():
    sid = request.json.get('id')
    if sid in processos:
        processos[sid].terminate()
        del processos[sid]
    remover_bot_db(sid)
    if sid in logs_web:
        del logs_web[sid]
    return jsonify({"ok": True})

# ════════════════════════════════════════════════════════════════════════
# FUNÇÃO CLOUDFLARE TUNNEL E DETECÇÃO DE PORTA
# ════════════════════════════════════════════════════════════════════════
def obter_porta_livre(porta_inicial=5006):
    """Verifica se a porta está em uso e incrementa até encontrar uma livre."""
    porta = porta_inicial
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('0.0.0.0', porta))
                return porta
            except OSError:
                porta += 1

def iniciar_cloudflare(porta):
    try:
        url = try_cloudflare(port=int(porta))
        print("=" * 50)
        print("LINK DO PAINEL:")
        print(url)
        print("=" * 50)
    except Exception as e:
        print(f"Erro ao iniciar Cloudflare: {e}")

# ════════════════════════════════════════════════════════════════════════
# PONTO DE ENTRADA
# ════════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    print("SHIELD V37 - IQ OPTION + TELEGRAM (MULTI-USUÁRIO SEGURO)")
    
    # Descobre uma porta livre automaticamente
    PORTA_USADA = obter_porta_livre(5006)
    print(f"✅ Porta local definida: {PORTA_USADA}")
    
    manager = multiprocessing.Manager()
    logs_web = manager.dict()
    
    # Inicia o túnel usando a porta que descobrimos ser livre
    threading.Thread(target=lambda: iniciar_cloudflare(PORTA_USADA), daemon=True).start()
    
    # Roda o Flask na porta livre
    app.run(host="0.0.0.0", port=PORTA_USADA, debug=False)
