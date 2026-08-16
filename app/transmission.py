import os
import json
import tempfile
import base64
from typing import Optional
from transmission_rpc import Client, TransmissionError


class TransmissionClient:
    def __init__(self, host: str, port: int, username: str = "", 
                 password: str = "", timeout: int = 30):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout

    def connect(self) -> Client:
        """Создаёт и возвращает клиент Transmission RPC."""
        try:
            client = Client(
                host=self.host,
                port=self.port,
                username=self.username if self.username else None,
                password=self.password if self.password else None,
                timeout=self.timeout
            )
            client.session_stats()
            return client
        except TransmissionError as e:
            raise Exception(f"Ошибка подключения к Transmission: {e}")
        except Exception as e:
            raise Exception(f"Не удалось подключиться к Transmission: {e}")

    def test_connection(self) -> tuple:
        """Проверяет соединение. Возвращает (ok, message)."""
        try:
            client = self.connect()
            session = client.get_session()
            version = session.version
            return True, f"Подключено к Transmission {version}"
        except Exception as e:
            return False, str(e)

    def add_torrent(self, torrent_data: bytes, download_dir: str = None,
                    paused: bool = False) -> dict:
        """Добавляет торрент в Transmission.
        
        Пробует несколько способов в зависимости от версии transmission-rpc:
        1. bytes напрямую как первый аргумент (v4+)
        2. file:// URI через временный файл
        3. Параметр metainfo (v3)
        
        Args:
            torrent_data: содержимое .torrent файла (bytes)
            download_dir: путь для скачивания (None = использовать дефолт)
            paused: добавить на паузу
        
        Returns:
            dict с информацией о торренте
        """
        client = self.connect()
        torrent_b64 = base64.b64encode(torrent_data).decode('ascii')
        
        # Собираем общие параметры
        common_kwargs = {'paused': paused}
        if download_dir:
            common_kwargs['download_dir'] = download_dir
        
        last_error = None
        
        # Способ 1: bytes напрямую (transmission-rpc v4+)
        try:
            print("[transmission] Attempt 1: bytes as positional argument")
            torrent = client.add_torrent(torrent_data, **common_kwargs)
            return self._build_result(torrent, "bytes")
        except TypeError as e:
            if "unexpected keyword" in str(e) or "positional" in str(e):
                print(f"[transmission]   Attempt 1 failed: {e}")
                last_error = e
            else:
                raise
        except Exception as e:
            print(f"[transmission]   Attempt 1 error: {e}")
            last_error = e
        
        # Способ 2: file:// URI через временный файл
        tmp_path = None
        try:
            print("[transmission] Attempt 2: file:// URI via temp file")
            with tempfile.NamedTemporaryFile(mode='wb', suffix='.torrent', delete=False) as tmp:
                tmp.write(torrent_data)
                tmp_path = tmp.name
            
            file_uri = f"file://{tmp_path}"
            torrent = client.add_torrent(file_uri, **common_kwargs)
            result = self._build_result(torrent, "file://")
            return result
        except TypeError as e:
            if "unexpected keyword" in str(e) or "positional" in str(e):
                print(f"[transmission]   Attempt 2 failed: {e}")
                last_error = e
            else:
                raise
        except Exception as e:
            print(f"[transmission]   Attempt 2 error: {e}")
            last_error = e
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
        
        # Способ 3: base64 как первый позиционный аргумент
        try:
            print("[transmission] Attempt 3: base64 as positional argument")
            torrent = client.add_torrent(torrent_b64, **common_kwargs)
            return self._build_result(torrent, "base64")
        except Exception as e:
            print(f"[transmission]   Attempt 3 error: {e}")
            last_error = e
        
        # Способ 4: явный параметр torrent=bytes
        try:
            print("[transmission] Attempt 4: torrent=bytes kwarg")
            torrent = client.add_torrent(torrent=torrent_data, **common_kwargs)
            return self._build_result(torrent, "torrent=bytes")
        except Exception as e:
            print(f"[transmission]   Attempt 4 error: {e}")
            last_error = e
        
        # Если ничего не сработало — проверяем какие параметры принимает add_torrent
        import inspect
        sig = inspect.signature(client.add_torrent)
        print(f"[transmission] add_torrent signature: {sig}")
        raise Exception(f"Не удалось добавить торрент ни одним способом. "
                        f"Последняя ошибка: {last_error}. "
                        f"Сигнатура add_torrent: {sig}")

    def _build_result(self, torrent, method: str) -> dict:
        """Собирает информацию о добавленном торренте."""
        print(f"[transmission] Added via method '{method}': {torrent.name}")
        return {
            'hash': torrent.hashString,
            'name': torrent.name,
            'size': torrent.total_size,
            'status': 'added',
            'method': method
        }

    def get_torrent_status(self, torrent_hash: str) -> Optional[dict]:
        """Получает статус торрента по хэшу."""
        try:
            client = self.connect()
            torrent = client.get_torrent(torrent_hash)
            
            return {
                'hash': torrent.hashString,
                'name': torrent.name,
                'status': torrent.status,
                'progress': torrent.progress,
                'size': torrent.total_size,
                'downloaded': torrent.downloaded_ever,
                'rate_down': torrent.rate_download,
                'rate_up': torrent.rate_upload,
                'eta': torrent.eta.seconds if torrent.eta else None,
                'is_finished': torrent.is_finished
            }
        except TransmissionError:
            return None
        except Exception as e:
            print(f"[transmission] Error getting status: {e}")
            return None

    def remove_torrent(self, torrent_hash: str, delete_data: bool = False) -> bool:
        """Удаляет торрент из Transmission."""
        try:
            client = self.connect()
            client.remove_torrent(torrent_hash, delete_data=delete_data)
            return True
        except Exception as e:
            print(f"[transmission] Error removing torrent: {e}")
            return False