import base64
from pathlib import Path
from Crypto.Cipher import AES
from Crypto.Util import Counter

def decrypt_security_token(security_token: str):
    """
    Decrypts security token into (key, nonce) pair using master key.
    Source: yaronzz/Tidal-Media-Downloader & RedSea.
    """
    master_key = base64.b64decode('UIlTTEMmmLfGowo/UC60x2H45W6MdGgTRfo/umg4754=')
    sec_bytes = base64.b64decode(security_token)
    
    iv = sec_bytes[:16]
    encrypted_token = sec_bytes[16:]
    
    cipher = AES.new(master_key, AES.MODE_CBC, iv)
    decrypted_token = cipher.decrypt(encrypted_token)
    
    key = decrypted_token[:16]
    nonce = decrypted_token[16:24]
    return key, nonce

def decrypt_file(src_path: Path, dest_path: Path, key: bytes, nonce: bytes):
    """
    Decrypts an AES-CTR encrypted stream file in chunks.
    """
    nonce_val = int.from_bytes(nonce, 'big')
    ctr = Counter.new(64, prefix=nonce, initial_value=0)
    cipher = AES.new(key, AES.MODE_CTR, counter=ctr)
    
    with open(src_path, 'rb') as f_in, open(dest_path, 'wb') as f_out:
        while True:
            chunk = f_in.read(1024 * 64)
            if not chunk:
                break
            f_out.write(cipher.decrypt(chunk))
