"""Stable arm64 __TEXT,__text identity despite signature-only bundle changes."""
import hashlib
import struct
from pathlib import Path

def code_sha256(path):
 data=Path(path).read_bytes(); magic=struct.unpack_from('>I',data)[0]; off=0
 if magic in (0xcafebabe,0xcafebabf):
  count=struct.unpack_from('>I',data,4)[0]; wide=magic==0xcafebabf
  for i in range(count):
   values=struct.unpack_from('>IIQQII' if wide else '>IIIII',data,8+i*(32 if wide else 20))
   if values[0]==0x100000c: off=values[2]; break
  else: raise ValueError('arm64_missing')
 if struct.unpack_from('<I',data,off)[0]!=0xfeedfacf: raise ValueError('not_macho64')
 ncmd=struct.unpack_from('<I',data,off+16)[0]; pos=off+32
 for _ in range(ncmd):
  cmd,size=struct.unpack_from('<II',data,pos)
  if size<8: raise ValueError('malformed_load_command')
  if cmd==0x19:
   count=struct.unpack_from('<I',data,pos+64)[0]
   for j in range(count):
    section=pos+72+j*80
    name=data[section:section+16].rstrip(b'\0'); seg=data[section+16:section+32].rstrip(b'\0')
    if name==b'__text' and seg==b'__TEXT':
     length=struct.unpack_from('<Q',data,section+40)[0]; start=off+struct.unpack_from('<I',data,section+48)[0]
     if start+length>len(data): raise ValueError('section_out_of_bounds')
     return hashlib.sha256(data[start:start+length]).hexdigest()
  pos+=size
 raise ValueError('code_section_missing')
if __name__=='__main__':
 import sys
 print(code_sha256(sys.argv[1]))
