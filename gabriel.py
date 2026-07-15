import requests

from datetime import datetime

import sys, os, multiprocessing, time, threading, json

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

# BANCO DE DADOS SQLITE (COM TIMEOUT)

# ════════════════════════════════════════════════════════════════════════

DB_PATH = "shield_bots.db"



def init_database():

    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)

    cursor = conn.cursor()

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

# CONFIGURAÇÃO GIST

# ════════════════════════════════════════════════════════════════════════

URL_SISTEMA_GESTAO = "https://gist.githubusercontent.com/Brabomax/97ed147c10843a1c6f2b923df8243a65/raw/gistfile1.txt"



def verificar_acesso_remoto(email_digitado, proxies=None):

    try:

        r = requests.get(URL_SISTEMA_GESTAO, timeout=10, proxies=proxies)

        if r.status_code != 200:

            return True, "️ Offline"

        for linha in r.text.splitlines():

            if '|' in linha:

                email_l, data_exp = linha.split('|')

                if email_digitado.strip().lower() == email_l.strip().lower():

                    try:

                        if datetime.now() < datetime.strptime(data_exp.strip(), '%Y-%m-%d'):

                            return True, "✅ Ativo"

                    except Exception:

                        continue

        return False, " Negado"

    except Exception:

        return True, "⚠️ Offline"



# ════════════════════════════════════════════════════════════════════════

# FUNÇÕES PARA SALVAR/CARREGAR CREDENCIAIS

# ════════════════════════════════════════════════════════════════════════

def salvar_credenciais(email, senha):

    try:

        with open(f"user_{email.replace('@', '_at_')}.txt", "w") as f:

            f.write(senha)

        return True

    except:

        return False



def carregar_senha(email):

    try:

        with open(f"user_{email.replace('@', '_at_')}.txt", "r") as f:

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

# LOOP PRINCIPAL COM LOGS DETALHADOS E CONTADOR DE FALHAS

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

# FLASK e rotas

# ════════════════════════════════════════════════════════════════════════

app = Flask(__name__)



logs_web = {} 

processos = {}

tg_sessions_storage = {}

manager = None



@app.route('/tg_listar_grupos', methods=['POST'])

def tg_listar_grupos():

    data = request.json

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

                tg_sessions_storage[phone] = {

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

    api_id = data.get('api_id')

    api_hash = data.get('api_hash')

    phone = data.get('phone')

    code = data.get('code')

    session = data.get('session', '')

    dados_salvos = tg_sessions_storage.get(phone, {})

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

            if phone in tg_sessions_storage:

                del tg_sessions_storage[phone]

            return {"ok": True, "grupos": grupos, "session": sess_str}

        except Exception as e:

            try: await client.disconnect()

            except: pass

            return {"ok": False, "erro": str(e)}



    loop = asyncio.new_event_loop()

    res = loop.run_until_complete(_run())

    loop.close()

    return jsonify(res)



# ════════════════════════════════════════════════════════════════════════

# HTML COMPLETO

# ════════════════════════════════════════════════════════════════════════

HTML_SISTEMA = """

<!DOCTYPE html>

<html>

<head>

<meta name="viewport" content="width=device-width, initial-scale=1">

<title>Shield V37 IQ Option + Telegram</title>

<style>

body { background: #0b0e11; color: #e1e1e1; font-family: 'Segoe UI', sans-serif; padding: 10px; }

.box { background: #151a21; padding: 15px; border-radius: 8px; margin-bottom: 10px; border: 1px solid #2b3139; }

.box-tg { background: #0d1625; padding: 15px; border-radius: 8px; margin-bottom: 10px; border: 1px solid #1a4a8a; }

.box-tg h4 { color: #2196F3; margin: 0 0 12px 0; font-size: 15px; }

.placar { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; background: #00c853; padding: 12px; border-radius: 8px; color: #000; font-weight: 900; text-align:center; margin-bottom: 15px; }

input, select, textarea { width: 100%; padding: 10px; margin: 5px 0; background: #1e2329; color: white; border: 1px solid #333; border-radius: 4px; box-sizing: border-box; }

textarea { height: 80px; font-family: monospace; font-size: 12px; }

.btn-on  { background: #00c853; color: black; border: none; padding: 15px; width: 100%; border-radius: 5px; font-weight: bold; cursor:pointer; font-size: 16px; }

.btn-off { background: #f44336; color: white; border: none; padding: 12px; width: 100%; border-radius: 5px; cursor:pointer; margin-top: 8px; }

.btn-tg  { background: #2196F3; color: white; border: none; padding: 10px 18px; border-radius: 5px; cursor:pointer; font-weight: bold; font-size: 13px; margin-top: 6px; width: 100%; }

.btn-tg:disabled { background: #555; cursor: default; }

#monitor { background: #000; color: #00ff41; height: 200px; overflow-y: scroll; padding: 10px; font-family: monospace; font-size: 12px; border-radius: 5px; border: 1px solid #333; margin-top: 15px; }

.flex { display: flex; gap: 8px; align-items: flex-end; }

.grid-est { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 5px; margin-top: 10px; }

.est-item { background: #1e2329; padding: 5px; border-radius: 3px; font-size: 10px; display: flex; align-items: center; }

.check-row { display: flex; flex-wrap: wrap; gap: 10px; background: #1e2329; padding: 10px; border-radius: 5px; margin-top: 10px; font-size: 13px; align-items: center; }

#status_panel { background:#1e2329; padding:10px; border-radius:8px; margin-bottom:10px; text-align:center; font-size:13px; }

.bot-status { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 5px; }

.status-running { background: #00c853; }

.status-stopped { background: #f44336; }

.status-offline { background: #ff9800; }

.status-travado { background: #9c27b0; animation: blink 1s infinite; }

@keyframes blink { 50% { opacity: 0.3; } }

.badge-iq { background:#4CAF50; padding:2px 8px; border-radius:12px; font-size:11px; margin-left:8px; }

.badge-tg { background:#1565C0; color:#fff; font-size:10px; padding:2px 7px; border-radius:10px; margin-left:6px; }

.tg-lbl { font-size:12px; color:#90CAF9; display:block; margin-bottom:2px; }

.pares-btn { background: #2b3139; border: 1px solid #00c853; color: #00c853; padding: 5px 10px; border-radius: 4px; cursor: pointer; margin-top: 5px; font-size: 11px; }

.pares-btn:hover { background: #00c853; color: #000; }

.tg-status { font-size:12px; color:#aaa; margin-top:8px; }

.tg-code-area { margin-top:8px; display:none; }

.tg-group-select { margin-top:8px; display:none; }

.rec-box { background:#2a1a0a; border:1px solid #ff5722; border-radius:5px; padding:8px; margin-top:8px; }

.badge-rec { background:#ff5722; color:#fff; font-size:10px; padding:2px 7px; border-radius:10px; margin-left:6px; font-weight:bold; }

.btn-reconnect { background: #ff9800; color: black; border: none; padding: 10px; width: 100%; border-radius: 5px; cursor: pointer; margin-top: 5px; font-weight: bold; }

.loss-box { background:#1a0a2a; border:1px solid #9c27b0; border-radius:5px; padding:8px; margin-top:8px; }

.badge-loss { background:#9c27b0; color:#fff; font-size:10px; padding:2px 7px; border-radius:10px; margin-left:6px; font-weight:bold; }

.ciclos-box { background:#0a1a2a; border:1px solid #00bcd4; border-radius:5px; padding:10px; margin-top:10px; }

.badge-ciclos { background:#00bcd4; color:#000; font-size:10px; padding:2px 7px; border-radius:10px; margin-left:6px; font-weight:bold; }

.ciclo-row { display: grid; grid-template-columns: 60px 1fr 1fr 1fr 1fr 50px; gap: 5px; margin-bottom: 5px; align-items: center; }

.ciclo-row input { padding: 6px; font-size: 12px; }

.ciclo-num { text-align: center; font-weight: bold; color: #00bcd4; font-size: 14px; }

.btn-add-ciclo { background: #00bcd4; color: #000; border: none; padding: 8px 15px; border-radius: 4px; cursor: pointer; font-weight: bold; margin-right: 5px; }

.btn-save-ciclos { background: #00c853; color: #000; border: none; padding: 8px 15px; border-radius: 4px; cursor: pointer; font-weight: bold; }

.btn-del-ciclo { background: #f44336; color: white; border: none; padding: 5px 10px; border-radius: 3px; cursor: pointer; }

.timeframe-box { background:#1a2a0a; border:1px solid #8bc34a; border-radius:5px; padding:8px; margin-top:8px; }

.badge-tf { background:#8bc34a; color:#000; font-size:10px; padding:2px 7px; border-radius:10px; margin-left:6px; font-weight:bold; }

.velas-box { background:#2a0a0a; border:1px solid #ff6b6b; border-radius:5px; padding:8px; margin-top:8px; }

.badge-velas { background:#ff6b6b; color:#fff; font-size:10px; padding:2px 7px; border-radius:10px; margin-left:6px; font-weight:bold; }

</style>

</head>

<body>

<h3 style="text-align:center; color:#00c853;">️ SHIELD V37 - IQ OPTION <span class="badge-iq">+ TELEGRAM</span> <span class="badge-rec">🔄 REC CONTÍNUA</span> <span class="badge-loss">🎯 OPERA APÓS LOSS</span> <span class="badge-ciclos">🔁 CICLOS</span> <span class="badge-tf">⏱️ TF SEPARADO</span> <span class="badge-velas">🛑 VELAS DOIDAS</span></h3>



<div id="status_panel">

    <span id="bot_status_indicator" class="bot-status status-offline"></span>

    <span id="bot_status_text">Nenhum bot ativo</span>

    <span id="ciclo_display" style="margin-left:10px; color:#00bcd4; font-weight:bold;"></span>

</div>



<div class="placar">

    <div>WINS: <span id="w_info">0</span></div>

    <div>LOSS: <span id="l_info">0</span></div>

    <div style="grid-column: span 2;">LUCRO: <span id="lucro_val">$0.00</span> | BANCA: <span id="banca_real">$0.00</span></div>

</div>



<div class="box">

    <input id="user" placeholder="E-mail IQ Option">

    <input id="pass" type="password" placeholder="Senha IQ Option">

    <select id="tipo">

        <option value="PRACTICE">CONTA PRÁTICA</option>

        <option value="REAL">CONTA REAL</option>

    </select>

    <div class="flex">

        <input id="sw" placeholder="Stop Win $" value="10">

        <input id="sl" placeholder="Stop Loss $" value="10">

    </div>

    <div class="flex">

        <label style="font-size:11px; width:100%;">Entrada $:

        <input id="ent" value="2.00"></label>

        <label style="font-size:11px; width:100%;">Assertividade %:

        <input id="min_rank" type="number" value="50"></label>

    </div>

    <div class="flex">

        <label style="font-size:11px; width:100%;">Sinais por horário:

        <select id="max_sinais_horario">

            <option value="1">1 sinal (melhor)</option>

            <option value="2">2 sinais</option>

            <option value="3">3 sinais</option>

            <option value="999" selected>Todos</option>

        </select></label>

        <label style="font-size:11px; width:100%;">% Recuperação:

        <select id="rec_percent_select" onchange="toggleRecCustom()">

            <option value="25">25%</option>

            <option value="50">50%</option>

            <option value="75">75%</option>

            <option value="100" selected>100%</option>

            <option value="150">150%</option>

            <option value="custom">Personalizado</option>

        </select></label>

        <input id="rec_percent_custom" type="number" value="100" style="display:none;" placeholder="%">

    </div>

    <div class="flex">

        <input id="par" value="EURUSD-OTC, GBPUSD-OTC, EURGBP-OTC">

        <label style="font-size:11px; width:120px;">💪 Força:

        <input id="min_forca" type="number" value="3"></label>

    </div>

    <div>

        <button class="pares-btn" onclick="setPares('EURUSD-OTC, GBPUSD-OTC, EURGBP-OTC')"> 3 Pares</button>

        <button class="pares-btn" onclick="setPares('EURUSD-OTC, GBPUSD-OTC, EURGBP-OTC, EURJPY-OTC, AUDUSD-OTC')">📊 5 Pares</button>

        <button class="pares-btn" onclick="setPares('EURUSD-OTC, GBPUSD-OTC, USDJPY-OTC, EURJPY-OTC, AUDUSD-OTC, NZDUSD-OTC')">📊 6 Pares</button>

    </div>

</div>



<div class="timeframe-box">

    <strong>⏱️ Timeframes Separados <span class="badge-tf">NOVO</span></strong>

    <div class="flex" style="margin-top:8px;">

        <label style="font-size:12px; width:100%;">📱 Telegram:

            <select id="tg_timeframe">

                <option value="1">M1 (1 min)</option>

                <option value="5" selected>M5 (5 min)</option>

                <option value="15">M15 (15 min)</option>

            </select>

        </label>

        <label style="font-size:12px; width:100%;">🤖 Estratégias:

            <select id="auto_timeframe">

                <option value="1" selected>M1 (1 min)</option>

                <option value="5">M5 (5 min)</option>

            </select>

        </label>

    </div>

    <small style="color:#8bc34a;">Telegram opera em M5 | Estratégias automáticas em M1</small>

</div>



<div class="box-tg">

    <h4>📡 Sinais ao Vivo — Telegram <span class="badge-tg">ESTILO DERIV</span></h4>

    <label class="tg-lbl">Número de Telefone (ex: +5511999999999):</label>

    <input id="tg_phone" placeholder="+5511999999999" type="tel">

    <div class="flex">

        <div style="width:100%;">

            <label class="tg-lbl">API ID (my.telegram.org):</label>

            <input id="tg_api_id" placeholder="API ID" type="number">

        </div>

        <div style="width:100%;">

            <label class="tg-lbl">API Hash:</label>

            <input id="tg_api_hash" placeholder="API Hash" type="password">

        </div>

    </div>

    <button class="btn-tg" id="btn_buscar_grupos" onclick="buscarGrupos()">🔍 Conectar Telegram e Buscar Grupos</button>

    <div id="tg_code_area" class="tg-code-area">

        <label class="tg-lbl">Código recebido por SMS/Telegram:</label>

        <div class="flex">

            <input id="tg_verification_code" placeholder="Ex: 12345" type="text">

            <button class="btn-tg" style="width:auto; margin-top:0;" onclick="confirmarCodigo()">✅ Confirmar</button>

        </div>

    </div>

    <div id="tg_group_select" class="tg-group-select">

        <label class="tg-lbl">Grupo / Canal para escutar:</label>

        <select id="tg_groups_dropdown" onchange="selecionarGrupo()">

            <option value="">-- Selecione --</option>

        </select>

    </div>

    <div id="tg_status" class="tg-status">Insira seus dados e clique em "Conectar Telegram".</div>

    <input type="hidden" id="tg_session" value="">

    <div class="check-row" style="margin-top:10px;">

        <label><input type="checkbox" id="modo_telegram"> 📱 Operar por Sinais Telegram</label>

        <label><input type="checkbox" id="tg_sem_ranking"> 📱 Telegram sem Ranking</label>

        <label style="font-size:11px;color:#888;">Marque para ativar sinais ao vivo do Telegram</label>

    </div>

</div>



<div class="box">

    <strong>Lista de Sinais (Formato: HH:MM,PAR,DIR):</strong>

    <textarea id="lista_sinais" placeholder="Exemplo:&#10;14:30,EURUSD-OTC,CALL&#10;14:35,GBPUSD-OTC,PUT"></textarea>

    <div class="check-row">

        <label><input type="checkbox" id="modo_lista"> 📁 Operar Lista</label>

        <label><input type="checkbox" id="modo_catalogo" checked> Catalogador</label>

        <label><input type="checkbox" id="use_vwin"> Win Virtual</label>

        <label><input type="checkbox" id="use_rec"> Recuperação</label>

        <label><input type="checkbox" id="rec_continua">  Recuperação Contínua</label>

        <label><input type="checkbox" id="filtro_confluencia"> ✅ Tendência IA</label>

        <label><input type="checkbox" id="filtro_forca"> ✅ Força mínima</label>

        <label><input type="checkbox" id="filtro_antiloss"> ✅ Anti-Loss</label>

        <label><input type="checkbox" id="filtro_volatilidade"> ✅ Volatilidade</label>

        <label><input type="checkbox" id="filtro_velas_doidas" onchange="toggleVelasDoidasConfig()"> 🛑 Bloquear Velas Doidas</label>

        <label><input type="checkbox" id="use_gale" checked> Martingale</label>

        <label><input type="checkbox" id="modo_inteligente"> 🧠 Modo Inteligente</label>

        <label><input type="checkbox" id="usar_5vela"> 🎯 5ª Vela</label>

        <label><input type="checkbox" id="opera_apos_loss" onchange="toggleLossConfig()"> 🎯 Opera Após Loss</label>

        <label><input type="checkbox" id="usar_ciclos" onchange="toggleCiclos()"> 🔁 Gerenc. por Ciclos</label>

    </div>



    <div class="velas-box" id="velas_doidas_config" style="display:none;">

        <div class="flex">

            <label style="font-size:12px; width:100%;">Fator Pavio (x corpo):

            <input id="fator_pavio" type="number" value="2.5" step="0.1" min="1.5" max="5" style="width:80px;"></label>

            <label style="font-size:12px; width:100%;">Máx velas doidas:

            <input id="max_pavios_permitidos" type="number" value="1" min="0" max="3" style="width:80px;"></label>

        </div>

        <small style="color:#ff6b6b;">🛑 Bloqueia operação se houver mais de X velas com pavio > Yx o corpo</small>

    </div>



    <div class="loss-box" id="loss_config" style="display:none;">

        <div class="flex">

            <label style="font-size:12px; width:100%;">Quantidade de LOSS seguidos:

            <input id="loss_target" type="number" value="2" min="1" max="10" style="width:80px;"></label>

            <label style="font-size:12px; width:100%;">Reset após X WINS REAIS:

            <input id="win_reset_target" type="number" value="3" min="1" max="20" style="width:80px;"></label>

        </div>

        <small style="color:#ce93d8;">🎯 Quando ativado, o bot só opera após X LOSS seguidos. Após pegar Y WINS REAIS, volta a esperar os LOSS novamente.</small>

    </div>



    <div class="ciclos-box" id="ciclos_config" style="display:none;">

        <strong>🔁 Gerenciamento por Ciclos Progressivos</strong>

        <small style="color:#80deea; display:block; margin:5px 0;">

            ✅ WIN em qualquer momento → Volta ao Ciclo 1<br>

            ❌ Perde ciclo inteiro (todas as mãos) → Avança pro próximo ciclo

        </small>

        <div style="margin-top:8px; font-size:11px; color:#aaa;">

            <div class="ciclo-row" style="font-weight:bold; color:#00bcd4;">

                <div>Ciclo</div>

                <div>Entrada $</div>

                <div>G1 $</div>

                <div>G2 $</div>

                <div>G3 $</div>

                <div>Ação</div>

            </div>

            <div id="lista_ciclos"></div>

        </div>

        <div style="margin-top:10px;">

            <button class="btn-add-ciclo" onclick="adicionarCiclo()">➕ Adicionar Ciclo</button>

            <button class="btn-save-ciclos" onclick="salvarCiclos()">💾 Salvar Ciclos</button>

        </div>

        <small id="ciclos_count" style="color:#00c853; display:block; margin-top:5px;">0 ciclo(s) configurado(s)</small>

    </div>



    <div class="rec-box" id="rec_continua_config" style="display:none;">

        <div class="flex">

            <label style="font-size:12px; width:100%;">Meta de Lucro Extra:

            <input id="rec_continua_meta" type="number" value="30" step="5" style="width:80px;"> %</label>

            <label style="font-size:12px; width:100%;">Fator Agressividade:

            <input id="rec_continua_fator" type="number" value="1.2" step="0.1" style="width:80px;"> x</label>

        </div>

        <small style="color:#ffaa66;">🔄 Quando ativado, busca recuperar prejuízo + lucro extra de X% a cada operação</small>

    </div>



    <div class="flex" style="margin-top:10px;">

        <select id="vwin_num">

            <option value="1">1 Win Virtual</option>

            <option value="2">2 Wins Virtuais</option>

        </select>

        <select id="max_gale">

            <option value="1">G1</option>

            <option value="2" selected>G2</option>

            <option value="3">G3</option>

        </select>

        <label style="font-size:10px; width:80px;">Fator Gale %:

        <input id="fator_gale" type="number" value="100"></label>

    </div>

</div>



<div class="box">

    <strong>Estratégias Ativas:</strong>

    <div class="grid-est">

        <div class="est-item"><input type="checkbox" class="est" value="MM" checked> Milhão</div>

        <div class="est-item"><input type="checkbox" class="est" value="PM" checked> Master</div>

        <div class="est-item"><input type="checkbox" class="est" value="M1" checked> MHI 1</div>

        <div class="est-item"><input type="checkbox" class="est" value="M2" checked> MHI 2</div>

        <div class="est-item"><input type="checkbox" class="est" value="MHI3" checked> MHI 3</div>

        <div class="est-item"><input type="checkbox" class="est" value="FL" checked> Fluxo</div>

        <div class="est-item"><input type="checkbox" class="est" value="TG" checked> Torre</div>

        <div class="est-item"><input type="checkbox" class="est" value="P23" checked> Padrão 23</div>

        <div class="est-item"><input type="checkbox" class="est" value="REV" checked> Reversão</div>

        <div class="est-item"><input type="checkbox" class="est" value="C3" checked> Padrão C3</div>

        <div class="est-item"><input type="checkbox" class="est" value="V1" checked> Vizinhança</div>

        <div class="est-item"><input type="checkbox" class="est" value="TRI" checked> Três Viz.</div>

    </div>

</div>



<button class="btn-on" onclick="acao('ligar')">▶️ INICIAR SHIELD + TELEGRAM</button>

<button class="btn-reconnect" onclick="reconectar()">🔄 RECONECTAR</button>

<button class="btn-off" onclick="acao('parar')">🛑 PARAR TUDO</button>



<div id="monitor">Aguardando comando...</div>



<script>

let ID = localStorage.getItem('shield_bot_id');

if (!ID) {

    ID = "U" + Math.random().toString(36).substring(7);

    localStorage.setItem('shield_bot_id', ID);

}



let ciclosConfig = [];

let cicloAtual = 1;



function setPares(valor) { document.getElementById('par').value = valor; }



function toggleRecCustom() {

    let select = document.getElementById('rec_percent_select');

    let custom = document.getElementById('rec_percent_custom');

    if (select.value === 'custom') {

        custom.style.display = 'block';

    } else {

        custom.style.display = 'none';

        custom.value = select.value;

    }

}



function toggleLossConfig() {

    let checkbox = document.getElementById('opera_apos_loss');

    let config = document.getElementById('loss_config');

    config.style.display = checkbox.checked ? 'block' : 'none';

}



function toggleCiclos() {

    let checkbox = document.getElementById('usar_ciclos');

    let config = document.getElementById('ciclos_config');

    config.style.display = checkbox.checked ? 'block' : 'none';

    if (checkbox.checked && ciclosConfig.length === 0) {

        adicionarCiclo();

        adicionarCiclo();

        adicionarCiclo();

    }

}



function toggleVelasDoidasConfig() {

    let checkbox = document.getElementById('filtro_velas_doidas');

    let config = document.getElementById('velas_doidas_config');

    config.style.display = checkbox.checked ? 'block' : 'none';

}



function adicionarCiclo() {

    let container = document.getElementById('lista_ciclos');

    let num = container.children.length + 1;

    let row = document.createElement('div');

    row.className = 'ciclo-row';

    row.innerHTML = `

        <div class="ciclo-num">${num}</div>

        <div><input type="number" class="ciclo_entrada" value="${num === 1 ? 2 : num === 2 ? 8 : 32}" step="0.5" style="width:100%;"></div>

        <div><input type="number" class="ciclo_g1" value="${num === 1 ? 4 : num === 2 ? 16 : 80}" step="0.5" style="width:100%;"></div>

        <div><input type="number" class="ciclo_g2" value="0" step="0.5" style="width:100%;"></div>

        <div><input type="number" class="ciclo_g3" value="0" step="0.5" style="width:100%;"></div>

        <div><button class="btn-del-ciclo" onclick="removerCiclo(this)">🗑️</button></div>

    `;

    container.appendChild(row);

    atualizarContadorCiclos();

}



function removerCiclo(btn) {

    let row = btn.closest('.ciclo-row');

    row.remove();

    renumerarCiclos();

    atualizarContadorCiclos();

}



function renumerarCiclos() {

    let rows = document.querySelectorAll('#lista_ciclos .ciclo-row');

    rows.forEach((row, idx) => {

        row.querySelector('.ciclo-num').textContent = idx + 1;

    });

}



function atualizarContadorCiclos() {

    let count = document.querySelectorAll('#lista_ciclos .ciclo-row').length;

    document.getElementById('ciclos_count').textContent = count + ' ciclo(s) configurado(s)';

}



function salvarCiclos() {

    ciclosConfig = [];

    let rows = document.querySelectorAll('#lista_ciclos .ciclo-row');

    rows.forEach(row => {

        ciclosConfig.push({

            entrada: parseFloat(row.querySelector('.ciclo_entrada').value) || 2,

            g1: parseFloat(row.querySelector('.ciclo_g1').value) || 0,

            g2: parseFloat(row.querySelector('.ciclo_g2').value) || 0,

            g3: parseFloat(row.querySelector('.ciclo_g3').value) || 0

        });

    });

    cicloAtual = 1;

    alert('✅ ' + ciclosConfig.length + ' ciclos salvos!');

    let mon = document.getElementById('monitor');

    mon.innerHTML += '\\n🔁 CICLOS SALVOS: ' + ciclosConfig.length + ' ciclo(s)\\n';

    mon.scrollTop = mon.scrollHeight;

}



function reconectar() {

    fetch('/reconectar/' + ID, { method: 'POST' })

        .then(r => r.json())

        .then(res => {

            let mon = document.getElementById('monitor');

            if (res.ok) mon.innerHTML += "\\n✅ RECONECTADO!\\n";

            else mon.innerHTML += "\\n❌ " + (res.erro || "Erro") + "\\n";

            mon.scrollTop = mon.scrollHeight;

        });

}



let tgAuthInProgress = false;



function buscarGrupos() {

    if (tgAuthInProgress) return;

    let phone = document.getElementById('tg_phone').value.trim();

    let api_id = document.getElementById('tg_api_id').value.trim();

    let api_hash = document.getElementById('tg_api_hash').value.trim();

    if (!phone || !api_id || !api_hash) {

        document.getElementById('tg_status').innerHTML = '⚠️ Preencha telefone, API ID e API Hash.';

        document.getElementById('tg_status').style.color = '#ff9800';

        return;

    }

    tgAuthInProgress = true;

    document.getElementById('btn_buscar_grupos').disabled = true;

    document.getElementById('tg_status').innerHTML = '🔄 Conectando e enviando código...';

    document.getElementById('tg_status').style.color = '#2196F3';



    fetch('/tg_listar_grupos', {

        method: 'POST',

        headers: {'Content-Type': 'application/json'},

        body: JSON.stringify({

            phone: phone,

            api_id: api_id,

            api_hash: api_hash,

            session: document.getElementById('tg_session').value

        })

    })

    .then(r => r.json())

    .then(data => {

        if (data.error) {

            document.getElementById('tg_status').innerHTML = '❌ ' + data.error;

            document.getElementById('tg_status').style.color = '#f44336';

            tgAuthInProgress = false;

            document.getElementById('btn_buscar_grupos').disabled = false;

            return;

        }

        if (data.precisa_codigo) {

            document.getElementById('tg_status').innerHTML = '📨 Código enviado! Insira abaixo.';

            document.getElementById('tg_status').style.color = '#4CAF50';

            document.getElementById('tg_code_area').style.display = 'block';

            tgAuthInProgress = false;

            document.getElementById('btn_buscar_grupos').disabled = false;

        } else if (data.ok) {

            preencherGrupos(data.grupos, data.session);

        } else {

            document.getElementById('tg_status').innerHTML = '⚠️ Resposta inesperada.';

            tgAuthInProgress = false;

            document.getElementById('btn_buscar_grupos').disabled = false;

        }

    })

    .catch(err => {

        document.getElementById('tg_status').innerHTML = '❌ Erro: ' + err;

        document.getElementById('tg_status').style.color = '#f44336';

        tgAuthInProgress = false;

        document.getElementById('btn_buscar_grupos').disabled = false;

    });

}



function confirmarCodigo() {

    let code = document.getElementById('tg_verification_code').value.trim();

    if (!code) {

        document.getElementById('tg_status').innerHTML = '⚠️ Digite o código.';

        document.getElementById('tg_status').style.color = '#ff9800';

        return;

    }

    document.getElementById('tg_status').innerHTML = '🔄 Verificando código...';

    document.getElementById('tg_status').style.color = '#2196F3';



    fetch('/tg_confirmar_codigo', {

        method: 'POST',

        headers: {'Content-Type': 'application/json'},

        body: JSON.stringify({

            phone: document.getElementById('tg_phone').value.trim(),

            api_id: document.getElementById('tg_api_id').value.trim(),

            api_hash: document.getElementById('tg_api_hash').value.trim(),

            code: code,

            session: document.getElementById('tg_session').value

        })

    })

    .then(r => r.json())

    .then(data => {

        if (data.error) {

            document.getElementById('tg_status').innerHTML = '❌ ' + data.error;

            document.getElementById('tg_status').style.color = '#f44336';

            return;

        }

        if (data.ok) {

            preencherGrupos(data.grupos, data.session);

        } else {

            document.getElementById('tg_status').innerHTML = '️ Erro desconhecido.';

            document.getElementById('tg_status').style.color = '#ff9800';

        }

    })

    .catch(err => {

        document.getElementById('tg_status').innerHTML = '❌ Erro: ' + err;

        document.getElementById('tg_status').style.color = '#f44336';

    });

}



function preencherGrupos(groups, session_str) {

    let dropdown = document.getElementById('tg_groups_dropdown');

    dropdown.innerHTML = '<option value="">-- Selecione --</option>';

    groups.forEach(g => {

        let opt = document.createElement('option');

        opt.value = g.id;

        opt.textContent = g.nome + ' (ID: ' + g.id + ')';

        dropdown.appendChild(opt);

    });

    document.getElementById('tg_group_select').style.display = 'block';

    document.getElementById('tg_status').innerHTML = '✅ Autenticado! Selecione o grupo.';

    document.getElementById('tg_status').style.color = '#4CAF50';

    document.getElementById('tg_code_area').style.display = 'none';

    document.getElementById('tg_session').value = session_str || '';

    tgAuthInProgress = false;

    document.getElementById('btn_buscar_grupos').disabled = false;

}



function selecionarGrupo() {

    let dropdown = document.getElementById('tg_groups_dropdown');

    let selected = dropdown.value;

    if (selected) {

        document.getElementById('tg_status').innerHTML = '✅ Grupo selecionado: ' + dropdown.options[dropdown.selectedIndex].text;

        document.getElementById('tg_status').style.color = '#4CAF50';

    } else {

        document.getElementById('tg_status').innerHTML = 'Selecione um grupo.';

        document.getElementById('tg_status').style.color = '#aaa';

    }

}



document.getElementById('modo_telegram').addEventListener('change', function() {

    let status = document.getElementById('tg_status');

    if (this.checked) {

        let groupId = document.getElementById('tg_groups_dropdown').value;

        if (!groupId) {

            status.innerHTML = '⚠️ Selecione um grupo antes de ativar.';

            status.style.color = '#ff9800';

            this.checked = false;

            return;

        }

        status.innerHTML = '✅ Modo Telegram ativado. Sinais serão executados.';

        status.style.color = '#00e676';

    } else {

        status.innerHTML = 'ℹ️ Modo Telegram desativado.';

        status.style.color = '#aaa';

    }

});



function acao(t) {

    let d = { id: ID };

    if (t === 'ligar') {

        let ests = Array.from(document.querySelectorAll('.est:checked')).map(cb => cb.value);

        let recPercent = document.getElementById('rec_percent_custom').value;

        if (document.getElementById('rec_percent_select').value !== 'custom') {

            recPercent = document.getElementById('rec_percent_select').value;

        }

        Object.assign(d, {

            user: document.getElementById('user').value,

            pass: document.getElementById('pass').value,

            tipo: document.getElementById('tipo').value,

            par: document.getElementById('par').value,

            ent: document.getElementById('ent').value,

            sw: document.getElementById('sw').value || 10,

            sl: document.getElementById('sl').value || 10,

            min_rank: document.getElementById('min_rank').value || 50,

            min_forca: document.getElementById('min_forca').value || 3,

            max_sinais_horario: document.getElementById('max_sinais_horario').value,

            rec_percent: recPercent || 100,

            modo_lista: document.getElementById('modo_lista').checked,

            lista_sinais: document.getElementById('lista_sinais').value,

            modo_catalogo: document.getElementById('modo_catalogo').checked,

            use_vwin: document.getElementById('use_vwin').checked,

            vwin_num: document.getElementById('vwin_num').value,

            rec: document.getElementById('use_rec').checked,

            rec_continua: document.getElementById('rec_continua').checked,

            rec_continua_meta: document.getElementById('rec_continua_meta').value || 30,

            filtro_confluencia: document.getElementById('filtro_confluencia').checked,

            filtro_forca: document.getElementById('filtro_forca').checked,

            filtro_antiloss: document.getElementById('filtro_antiloss').checked,

            filtro_volatilidade: document.getElementById('filtro_volatilidade').checked,

            filtro_velas_doidas: document.getElementById('filtro_velas_doidas').checked,

            fator_pavio: document.getElementById('fator_pavio').value || 2.5,

            max_pavios_permitidos: document.getElementById('max_pavios_permitidos').value || 1,

            use_gale: document.getElementById('use_gale').checked,

            max_gale: document.getElementById('max_gale').value,

            fator_gale: document.getElementById('fator_gale').value || 100,

            estrategias: ests,

            modo_inteligente: document.getElementById('modo_inteligente').checked,

            usar_5vela: document.getElementById('usar_5vela').checked,

            modo_telegram: document.getElementById('modo_telegram').checked,

            tg_sem_ranking: document.getElementById('tg_sem_ranking').checked,

            tg_phone: document.getElementById('tg_phone').value,

            tg_api_id: document.getElementById('tg_api_id').value,

            tg_api_hash: document.getElementById('tg_api_hash').value,

            tg_group_id: document.getElementById('tg_groups_dropdown').value,

            tg_session: document.getElementById('tg_session').value,

            opera_apos_loss: document.getElementById('opera_apos_loss').checked,

            loss_target: document.getElementById('loss_target').value || 2,

            win_reset_target: document.getElementById('win_reset_target').value || 3,

            tg_timeframe: document.getElementById('tg_timeframe').value,

            auto_timeframe: document.getElementById('auto_timeframe').value,

            usar_ciclos: document.getElementById('usar_ciclos').checked,

            ciclos: ciclosConfig,

            ciclo_inicial: cicloAtual

        });

    }

    fetch('/' + t, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(d) });

}



setInterval(() => {

    fetch('/status/' + ID).then(r => r.json()).then(d => {

        if (d.msg) {

            let mon = document.getElementById('monitor');

            if (mon.innerHTML.length > 5000) mon.innerHTML = mon.innerHTML.slice(-2000);

            mon.innerHTML += d.msg;

            mon.scrollTop = mon.scrollHeight;

        }

        document.getElementById('w_info').innerText = d.wins || 0;

        document.getElementById('l_info').innerText = d.loss || 0;

        document.getElementById('lucro_val').innerText = '$' + (d.lucro_sessao || 0).toFixed(2);

        document.getElementById('banca_real').innerText = '$' + (d.banca_real || 0).toFixed(2);

        

        if (d.ciclo_atual) {

            document.getElementById('ciclo_display').innerText = '🔁 Ciclo: ' + d.ciclo_atual;

        }

        

        let statusSpan = document.getElementById('bot_status_text');

        let statusInd = document.getElementById('bot_status_indicator');

        if (d.status === 'rodando') {

            statusSpan.innerText = '✅ Bot em execução';

            statusInd.className = 'bot-status status-running';

        } else if (d.status === 'finalizado') {

            statusSpan.innerText = '⏹️ Bot finalizado (Sessão concluída. Reinicie para continuar)';

            statusInd.className = 'bot-status status-stopped';

        } else if (d.status === 'travado') {

            statusSpan.innerText = '❌ BOT TRAVOU! Reinicie';

            statusInd.className = 'bot-status status-travado';

        } else {

            statusSpan.innerText = '🔄 Verificando...';

            statusInd.className = 'bot-status status-offline';

        }

    });

}, 1200);



setTimeout(() => {

    fetch('/check_bot/' + ID).then(r => r.json()).then(res => {

        if (res.exists && res.is_alive) {

            document.getElementById('monitor').innerHTML += "🔄 Bot existente detectado! Reconectando...\\n";

            reconectar();

        }

    });

}, 1000);

</script>

</body>

</html>

"""



# ════════════════════════════════════════════════════════════════════════

# ROTAS FLASK

# ════════════════════════════════════════════════════════════════════════

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



    if len(processos) > 0:

        return jsonify({"erro": "⚠️ Já existe um bot ativo!"})

    if sid in processos:

        return jsonify({"erro": "⚠️ Bot já rodando!"})



    email = d.get('user', '').strip()

    senha = d.get('pass', '').strip()



    if not senha:

        senha = carregar_senha(email)

        if not senha:

            return jsonify({"erro": "❌ Nenhuma senha encontrada. Informe a senha."})

        d['pass'] = senha

    else:

        salvar_credenciais(email, senha)



    ok, m = verificar_acesso_remoto(email)

    if not ok:

        return jsonify({"erro": m})



    salvar_bot_db(sid, email, d)



    p = multiprocessing.Process(target=loop_robo, args=(sid, d, logs_web))

    p.daemon = False

    p.start()

    processos[sid] = p



    return jsonify({"s": "ok"})



@app.route('/parar', methods=['POST'])

def parar():

    sid = request.json.get('id')

    if sid in processos:

        processos[sid].terminate()

        del processos[sid]

    remover_bot_db(sid)

    return jsonify({"ok": True})



# ════════════════════════════════════════════════════════════════════════

# FUNÇÃO CLOUDFLARE TUNNEL

# ════════════════════════════════════════════════════════════════════════

def iniciar_cloudflare():

    try:

        url = try_cloudflare(port=int(os.environ.get("PORT", 5006)))

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

    print("SHIELD V37 - IQ OPTION + TELEGRAM (ANTI-TRAVAMENTO)")

    print("Acesse: http://0.0.0.0:5006")

    

    manager = multiprocessing.Manager()

    logs_web = manager.dict()

    

    threading.Thread(target=iniciar_cloudflare, daemon=True).start()

    

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5006)), debug=False)
