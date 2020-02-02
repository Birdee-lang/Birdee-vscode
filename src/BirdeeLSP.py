from pygls.features import *
from pygls.server import LanguageServer
from pygls.types import *
from urllib.parse import unquote
import os
from urllib.parse import urlparse
from pygls.workspace import Document
import birdeec
import math
from threading import Lock

def uri2ospath(uri: str):
    p = urlparse(uri)
    return os.path.abspath(os.path.join(p.netloc, p.path))

server = LanguageServer()
txt = None

class Compiler:
    def __init__(self):
        self.mutex = Lock()
        self.changed= True
        self.last_status = False

    def __enter__(self):
        self.mutex.acquire()

    def __exit__(self, ty, value, traceback):
        self.mutex.release()
    
    def compile(self, uri, istr) -> bool:
        if not self.changed:
            return self.last_status
        e=None
        try:
            birdeec.clear_compile_unit()
            birdeec.top_level(istr)
            birdeec.process_top_level()
        except birdeec.TokenizerException:
            e=birdeec.get_tokenizer_error()
        except birdeec.CompileException:
            e=birdeec.get_compile_error()
        
        self.changed=False
        if not e:
            self.last_status=True
            server.publish_diagnostics(uri, [])
            return True
        else:
            self.last_status=False
            diag = Diagnostic(Range(
                Position(e.linenumber, e.pos+1), Position(e.linenumber, e.pos+2)
            ), e.msg)
            server.publish_diagnostics(uri, [diag])
            return False

compiler = Compiler()

def dbgprint(s):
    with open("d:\\birdeelsp.log", "a") as f:
        f.write(str(s)+"\n")

def find_ast_by_pos(pos: Position):
    tl = birdeec.get_top_level()
    res=[]
    def runfunc(ast: birdeec.StatementAST):
        if not ast:
            return
        #dbgprint(f"{str(ast)} {ast.pos.line} {ast.pos.pos}")
        if ast.pos.line == pos.line + 1 and ast.pos.pos >= pos.character + 1:
            res.append((ast.pos.pos - pos.character - 1, ast))
        ast.run(runfunc)
    toplevel=[] #list of (line distance, stmt)
    for a in tl:
        toplevel.append((abs(a.pos.line - pos.line -1), a))
    for a in sorted(toplevel, key = lambda x: x[0])[0: 4]: #get nearest 4 toplevel stmt, sorted by line
        runfunc(a[1]) # run into the AST to find the specific statement
    return sorted(res,  key=lambda x: x[0])

def sourcepos2position(pos: birdeec.SourcePos) -> Position:
    if not pos: return None
    return Position(pos.line - 1, pos.pos - 1)

def get_member_def_pos(mem: birdeec.MemberExprAST) -> birdeec.SourcePos:
    if mem.kind == birdeec.MemberExprAST.MemberType.FIELD:
        return mem.field.decl.pos
    elif mem.kind == birdeec.MemberExprAST.MemberType.FUNCTION or mem.kind == birdeec.MemberExprAST.MemberType.VIRTUAL_FUNCTION:
        return mem.func.decl.pos
    elif mem.kind == birdeec.MemberExprAST.MemberType.IMPORTED_DIM:
        return mem.imported_dim.pos
    elif mem.kind == birdeec.MemberExprAST.MemberType.IMPORTED_FUNCTION:
        return mem.imported_func.pos
    return None  

def get_def(uri, istr, pos: Position)->Position:
    ret=None
    with compiler:
        if not compiler.compile(uri, istr):
            return ret
        asts=find_ast_by_pos(pos)
        #dbgprint(str(asts))
        for ast in asts:
            impl=ast[1]
            if isinstance(impl, birdeec.LocalVarExprAST):
                ret=sourcepos2position(impl.vardef.pos)
                break
            if isinstance(impl, birdeec.ResolvedFuncExprAST):
                ret=sourcepos2position(impl.funcdef.pos)
                break
            if isinstance(impl, birdeec.MemberExprAST):
                ret=sourcepos2position(get_member_def_pos(impl))
                break	
    return ret

@server.feature(COMPLETION, trigger_characters=[','])
def completions(params: CompletionParams):
    """Returns completion items."""
    return CompletionList(False, [
        CompletionItem('"'),
        CompletionItem('['),
        CompletionItem(']'),
        CompletionItem('{'),
        CompletionItem(123)
    ])

@server.feature(DEFINITION)
def definitions(params: TextDocumentPositionParams):
    r=get_def(params.textDocument.uri, txt.source, params.position)
    if r:
        return Location(params.textDocument.uri, Range(
            r, Position(r.line, r.character+1)
        ))
    else:
        return None

@server.feature(TEXT_DOCUMENT_DID_CHANGE)
def didchange(params: DidChangeTextDocumentParams):
    with compiler:
        compiler.changed=True
    for ch in params.contentChanges:
        txt.apply_change(ch)

@server.feature(TEXT_DOCUMENT_DID_OPEN)
def didopen(params: DidOpenTextDocumentParams):
    global txt
    txt=Document(params.textDocument.uri)

@server.feature(TEXT_DOCUMENT_DID_SAVE)
def didsave(params: DidSaveTextDocumentParams):
    with compiler:
        compiler.compile(params.textDocument.uri, txt.source)


server.start_io()