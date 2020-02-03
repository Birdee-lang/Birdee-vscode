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
txt = dict()

class Compiler:
    def __init__(self):
        self.mutex = Lock()
        self.uri=None
        self.last_status = False
        self.last_compiled_source = None
        self.last_successful_source = dict()

    def __enter__(self):
        self.mutex.acquire()

    def __exit__(self, ty, value, traceback):
        self.mutex.release()
    
    def switch_to_last_successful(self, uri):
        if uri in self.last_successful_source:
            self.compile(uri, self.last_successful_source[uri])
        else:
            birdeec.clear_compile_unit()


    def compile(self, uri, istr) -> bool:
        if self.uri == uri and istr == self.last_compiled_source:
            return self.last_status
        e=None
        self.last_compiled_source = istr
        try:
            birdeec.clear_compile_unit()
            birdeec.top_level(istr)
            birdeec.process_top_level()
        except birdeec.TokenizerException:
            e=birdeec.get_tokenizer_error()
        except birdeec.CompileException:
            e=birdeec.get_compile_error()
        
        self.uri= uri
        self.changed=False
        if not e:
            self.last_status=True
            self.last_successful_source[uri] = istr
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

primitive_types=[
                    CompletionItem('byte', kind=CompletionItemKind.Class),
                    CompletionItem('int', kind=CompletionItemKind.Class),
                    CompletionItem('uint', kind=CompletionItemKind.Class),
                    CompletionItem('long', kind=CompletionItemKind.Class),
                    CompletionItem('ulong', kind=CompletionItemKind.Class),
                    CompletionItem('float', kind=CompletionItemKind.Class),
                    CompletionItem('double', kind=CompletionItemKind.Class),
                    CompletionItem('pointer', kind=CompletionItemKind.Class),
                ]

@server.feature(COMPLETION, trigger_characters=[' '])
def completions(params: CompletionParams):
    #bybass a bug (?) of pygls 
    if not hasattr(params.context, 'triggerKind'):
        return None
    if params.context.triggerKind==2:
        if params.context.triggerCharacter==' ':
            t: Document = txt[params.textDocument.uri]
            pos = params.position.character
            istr = t.source.split('\n')[params.position.line]
            if istr[pos-3:pos+1]=="as " or istr[pos-4:pos+1]=="new ":
                    with compiler:
                        compiler.switch_to_last_successful(params.textDocument.uri)
                    class_names = list(birdeec.get_classes(True).keys()) + list(birdeec.get_classes(False).keys())
                    functype_names = list(birdeec.get_functypes(True).keys()) + list(birdeec.get_functypes(False).keys())
                    cls_completion = [CompletionItem(name) for name in class_names]
                    func_completion = [CompletionItem(name, kind=CompletionItemKind.Function) for name in functype_names]
                    return CompletionList(False, primitive_types + cls_completion + func_completion)
    return None

@server.feature(DEFINITION)
def definitions(params: TextDocumentPositionParams):
    uri=params.textDocument.uri
    r=get_def(uri, txt[uri].source, params.position)
    if r:
        return Location(params.textDocument.uri, Range(
            r, Position(r.line, r.character+1)
        ))
    else:
        return None

@server.feature(TEXT_DOCUMENT_DID_CHANGE)
def didchange(params: DidChangeTextDocumentParams):
    for ch in params.contentChanges:
        txt[params.textDocument.uri].apply_change(ch)

@server.feature(TEXT_DOCUMENT_DID_OPEN)
def didopen(params: DidOpenTextDocumentParams):
    uri=params.textDocument.uri
    txt[uri]=Document(uri)
    
@server.feature(TEXT_DOCUMENT_DID_SAVE)
def didsave(params: DidSaveTextDocumentParams):
    with compiler:
        uri=params.textDocument.uri
        compiler.compile(uri, txt[uri].source)


server.start_io()