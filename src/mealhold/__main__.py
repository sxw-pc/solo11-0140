import argparse,json
from pathlib import Path
from . import meal,sample,cold,report,supplier
from .store import connect,read

COMMANDS={'meal':meal.run,'sample':sample.run,'cold':cold.run,'supplier':supplier.run,
          'composition':supplier.composition_run,'substitution':supplier.substitution_run,
          'sign':supplier.sign_run}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['meal','sample','cold','supplier','composition','substitution','label','sign','report','demo'])
    parser.add_argument('--db',default='mealhold.sqlite');parser.add_argument('--input')
    args=parser.parse_args();db=connect(args.db)
    try:
        if args.command=='report': result=report.build(db)
        elif args.command=='label':
            if not args.input:parser.error('--input required')
            result=supplier.label(db,read(args.input)['meal_id'])
        elif args.command=='demo':
            root=Path.cwd()/'fixtures';result=[]
            for command in ('meal','sample','cold'):
                with db:result.append(globals()[command].run(db,read(root/f'{command}-ok.json')))
            result={'runs':result,'report':report.build(db)}
        else:
            if not args.input:parser.error('--input required')
            with db:result=COMMANDS[args.command](db,read(args.input))
        print(json.dumps({'module_file':__file__,'result':result},ensure_ascii=False,indent=2))
    finally:db.close()
if __name__=='__main__':main()
