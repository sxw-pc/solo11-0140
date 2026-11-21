import argparse,json
from pathlib import Path
from . import meal,sample,cold,exposure,report
from .store import connect,read

DEMO=[('meal','meal-ok.json'),('sample','sample-ok.json'),('cold','cold-ok.json'),
      ('meal','meal-exposure-n.json'),('meal','meal-exposure-r.json'),
      ('exposure','exposure-ok.json'),('exposure','exposure-scan.json')]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['meal','sample','cold','exposure','report','demo'])
    parser.add_argument('--db',default='mealhold.sqlite');parser.add_argument('--input')
    args=parser.parse_args();db=connect(args.db)
    try:
        if args.command=='report': result=report.build(db)
        elif args.command=='demo':
            root=Path.cwd()/'fixtures';result=[]
            for command,name in DEMO:
                with db:result.append(globals()[command].run(db,read(root/name)))
            result={'runs':result,'report':report.build(db)}
        else:
            if not args.input:parser.error('--input required')
            with db:result=globals()[args.command].run(db,read(args.input))
        print(json.dumps({'module_file':__file__,'result':result},ensure_ascii=False,indent=2))
    finally:db.close()
if __name__=='__main__':main()
