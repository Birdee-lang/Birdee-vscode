from pygls.features import *
from pygls.server import LanguageServer
from pygls.types import *
from pygls.uris import from_fs_path, to_fs_path
from urllib.parse import unquote
import os
from pygls.workspace import Document
import birdeec
import math
import json
from threading import Lock
import bdutils

server = LanguageServer()
txt = dict()
root_path = None
source_root_path = None
cache_path = None

def dbgprint(s):
    with open("d:\\birdeelsp.log", "a") as f:
        f.write(str(s)+"\n")

def find_module_path(root, mod, ext):
    modname=list(mod)
    modname[-1] = modname[-1] + ext
    target_path = os.path.join(root, *modname)
    if os.path.exists(target_path):
        return target_path
    else:
        return None

class Compiler:
    def __init__(self):
        self.mutex = Lock()
        self.uri=None
        self.last_status = False
        self.last_compiled_source = None
        self.last_successful_source = dict() # str(uri) -> str(source)
        self.module_metadata = dict() # tuple[module_names] -> str(json)

    def __enter__(self):
        self.mutex.acquire()

    def __exit__(self, ty, value, traceback):
        self.mutex.release()
    
    def switch_to_last_successful(self, uri):
        if uri in self.last_successful_source:
            self.compile(uri, self.last_successful_source[uri])
        else:
            birdeec.clear_compile_unit()

    def on_exit(self):
        for mod in self.module_metadata:
            modname=list(mod)
            modname[-1] = modname[-1] + ".bmm"
            target_path = os.path.join(root_path, cache_path, *modname)
            tdir = os.path.dirname(target_path)
            if not os.path.exists(tdir):
                os.makedirs(tdir)
            with open(target_path, 'w') as f:
                f.write(self.module_metadata[mod])

    '''
    compile a module and its dependencies:
        if it has any uncompiled modules that we can find source in the workspace,
        compile it. Then get and store the dependencies' BMM metadata in memory.
        Then re-compile the module
    '''
    def _docompile(self, fspath, istr):
        def compileit():
            e = None
            birdeec.set_module_resolver(_module_resolver)
            try:
                birdeec.set_source_file_path(fspath)
                birdeec.clear_compile_unit()
                birdeec.top_level(istr)
                birdeec.process_top_level()
            except birdeec.TokenizerException:
                e=birdeec.get_tokenizer_error()
            except birdeec.CompileException:
                e=birdeec.get_compile_error()
            if not e:
                cur_module = tuple(birdeec.get_module_name().split("."))
                self.module_metadata[cur_module] = birdeec.get_metadata_json()
            return e

        while True:
            e=None
            can_recompile=True
            dependencies=[]
            def _module_resolver(modname, second_chance):
                tmod = tuple(modname)
                nonlocal can_recompile
                if not second_chance:
                    if tmod in self.module_metadata:
                        return ("$InMemoryModule", self.module_metadata[tmod])
                    else:
                        return None
                else:
                    root=os.path.join(root_path, source_root_path)
                    target_path = find_module_path(root, modname,".bdm")
                    if not target_path:
                        target_path = find_module_path(root, modname,".txt")
                    if target_path:
                        dependencies.append((tmod, target_path))
                    else:
                        can_recompile=False
                    return None
            e = compileit()
            if not e:
                return e
            if not can_recompile or len(dependencies)==0:
                return e
            # can re-compile
            # first compile dependencies
            for (mod,srcpath) in dependencies:
                if mod in self.module_metadata: 
                    # a dependency may be compiled in another dependency
                    continue
                src = ""
                with open(srcpath) as f:
                    src = f.read()
                sub_e = self._docompile(srcpath, src)
                if sub_e:
                    msg = "While compiling {}, an error occurs: {}".format(srcpath, sub_e.msg)
                    server.show_message(msg, MessageType.Error)
                    return e
        return e

    def compile(self, uri, istr) -> bool:
        if self.uri == uri and istr == self.last_compiled_source:
            return self.last_status

        self.last_compiled_source = istr
        fspath=to_fs_path(uri)
        e = self._docompile(fspath, istr)                
        self.uri= uri
        self.changed=False
        if not e:
            self.last_status=True
            self.last_successful_source[uri] = istr
            server.publish_diagnostics(uri, [])
            return True
        else:
            self.last_status=False
            if not birdeec.get_auto_completion_ast():
                diag = Diagnostic(Range(
                    Position(e.linenumber, e.pos+1), Position(e.linenumber, e.pos+2)
                ), e.msg)
                server.publish_diagnostics(uri, [diag])
            return False

compiler = Compiler()



def find_ast_by_pos(pos: Position, line_length: int):
    tl = birdeec.get_top_level()
    res=[]
    def runfunc(ast: birdeec.StatementAST):
        if not ast:
            return
        if ast.pos.line == pos.line + 1 and ast.pos.pos >= pos.character + 1:
            res.append((ast.pos.pos - pos.character - 1, ast))
        if ast.pos.line == pos.line + 2 and ast.pos.pos == 1:
            res.append((line_length - pos.character - 1, ast)) 
            #Birdee compiler marks the end of the expression, maybe in the next line
        ast.run(runfunc)
    have_candidate=False
    for idx, a in enumerate(tl):
        if a.pos.line >=  pos.line:
            if idx>0: runfunc(tl[idx-1])
            if idx>1: runfunc(tl[idx-2])
            runfunc(tl[idx])
            if idx+1 < len(tl): runfunc(tl[idx+1])
            have_candidate=True
            break
    if not have_candidate:
        runfunc(tl[len(tl)-1])
    return sorted(res,  key=lambda x: x[0])

def sourcepos2position(pos: birdeec.SourcePos, main_src_uri: str) -> (Position, str):
    if not pos: return None
    uri = from_fs_path(pos.source_path) if pos.source_idx != -1 else main_src_uri
    return (Position(pos.line - 1, pos.pos - 1), uri)

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

def get_def(uri, istr, pos: Position, line_length: int)-> (Position, str):
    ret=None
    with compiler:
        if not compiler.compile(uri, istr):
            return ret
        asts=find_ast_by_pos(pos, line_length)
        for ast in asts:
            impl=ast[1]
            if isinstance(impl, birdeec.LocalVarExprAST):
                ret=sourcepos2position(impl.vardef.pos, uri)
                break
            if isinstance(impl, birdeec.ResolvedFuncExprAST):
                ret=sourcepos2position(impl.funcdef.pos, uri)
                break
            if isinstance(impl, birdeec.MemberExprAST):
                ret=sourcepos2position(get_member_def_pos(impl), uri)
                break	
    return ret

def get_signature_help(expr: birdeec.AutoCompletionExprAST):
    rty: birdeec.ResolvedType = expr.resolved_type
    if rty.base==birdeec.BasicType.FUNC and rty.index_level==0:
        proto: birdeec.PrototypeAST = rty.get_detail()
        label = "function " + proto.name + "("
        cur_len=len(label)
        param_label_pos=[]
        param_label=[]
        for arg in proto.args:
            appender = "{} as {}".format(arg.name, arg.resolved_type)
            param_label.append(appender)
            param_label_pos.append((cur_len, cur_len + len(appender)))
            cur_len += len(appender) + 2 # 2 for ", "
        label = "{}{}) as {}".format(label, ", ".join(param_label), proto.return_type)
        parameters = [ParameterInformation(lab) for lab in param_label]
        return SignatureHelp([SignatureInformation(label, parameters=parameters)], 
            active_parameter=expr.parameter_number)

completions_for_array=[
                    CompletionItem('get_raw', kind=CompletionItemKind.Function),
                    CompletionItem('length', kind=CompletionItemKind.Function),
                ]

def get_completion_for_new(ty: birdeec.ResolvedType) -> CompletionList:
    if ty.index_level>0:
        return None
    detail = ty.get_detail()
    if isinstance(detail, birdeec.ClassAST):
        ret=[]
        def eachfunc(idx, length, field: birdeec.MemberFunctionDef):
            nonlocal ret
            ret.append(CompletionItem(field.decl.proto.name, kind=CompletionItemKind.Function))
        bdutils.foreach_method(detail,eachfunc)
        return CompletionList(False, ret)

def get_completion_for_type(ty: birdeec.ResolvedType) -> CompletionList:
    if ty.index_level>0:
        return CompletionList(False, completions_for_array)
    detail = ty.get_detail()
    if isinstance(detail, birdeec.ClassAST):
        ret=[]
        def eachfield(idx, length, field: birdeec.FieldDef):
            nonlocal ret
            ret.append(CompletionItem(field.decl.name, kind=CompletionItemKind.Field))
        bdutils.foreach_field(detail,eachfield)
        def eachfunc(idx, length, field: birdeec.MemberFunctionDef):
            nonlocal ret
            ret.append(CompletionItem(field.decl.proto.name, kind=CompletionItemKind.Function))
        bdutils.foreach_method(detail,eachfunc)
        return CompletionList(False, ret)
    if isinstance(detail, birdeec.ImportTree):
        ret=[]
        sub = detail.get_submodules()
        if len(sub)!=0:
            for name in sub:
                ret.append(CompletionItem(name, CompletionItemKind.Module))
        else:
            modu: birdeec.ImportedModule = detail.mod
            for name in modu.get_classmap():
                ret.append(CompletionItem(name, CompletionItemKind.Class))
            for name in modu.get_dimmap():
                ret.append(CompletionItem(name, CompletionItemKind.Variable))
            for name in modu.get_funcmap():
                ret.append(CompletionItem(name, CompletionItemKind.Function))             
            for name in modu.get_functypemap():
                ret.append(CompletionItem(name, CompletionItemKind.Class))

            for name in modu.get_imported_classmap():
                ret.append(CompletionItem(name, CompletionItemKind.Class))
            for name in modu.get_imported_dimmap():
                ret.append(CompletionItem(name, CompletionItemKind.Variable))
            for name in modu.get_imported_funcmap():
                ret.append(CompletionItem(name, CompletionItemKind.Function))             
            for name in modu.get_imported_functypemap():
                ret.append(CompletionItem(name, CompletionItemKind.Class))
        return CompletionList(False, ret)            

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

def get_module_metadata(mod)-> dict:
    tmod = tuple(mod)
    with compiler:
        if tmod in compiler.module_metadata:
            return json.loads(compiler.module_metadata[tmod])
    target=find_module_path(os.path.join(root_path, cache_path), mod, ".bmm")
    BIRDEE_HOME=os.environ['BIRDEE_HOME']
    if not target and BIRDEE_HOME:
        target=find_module_path(os.path.join(BIRDEE_HOME, "blib"), mod, ".bmm")
    if target:
        with open(target) as f:
            return json.load(f)

def get_completion_for_name_import(modname: str)-> CompletionList:
    if modname.endswith(':'):
        modname=modname[:-1]
    meta=get_module_metadata(modname.split("."))
    if meta:
        ret=[]
        for clz in meta["Classes"]:
            ret.append(CompletionItem(clz["name"], CompletionItemKind.Class))
        for var in meta["Variables"]:
            ret.append(CompletionItem(var["name"], CompletionItemKind.Variable))
        for var in meta["Functions"]:
            ret.append(CompletionItem(var["name"], CompletionItemKind.Function))        
        for var in meta["FunctionTemplates"]:
            if "name" in var:
                ret.append(CompletionItem(var["name"], CompletionItemKind.Function))
        for var in meta["FunctionTypes"]:
            ret.append(CompletionItem(var["name"], CompletionItemKind.Function))
        return CompletionList(False, ret) 

def array_starts_with(large, small):
    if len(large)<len(small):
        return False
    for i, itm in enumerate(small):
        if large[i]!=itm:
            return False
    return True

def get_completion_for_import(importcode: str)-> CompletionList:
    if importcode.endswith('.'):
        importcode=importcode[:-1]
    mod=importcode.split(".")
    CompletionItemKindFolder=19
    ret=dict()
    with compiler:
        for cmod in compiler.module_metadata:
            if array_starts_with(cmod, mod):
                if len(cmod)>=len(mod):
                    name=cmod[len(mod)]
                    if name not in ret:
                        ret[name]=CompletionItem(name, 
                            CompletionItemKind.Module if len(cmod)==len(mod)+1 else CompletionItemKindFolder)
    def find_next_in_directory(root: str, ext: str):
        path=os.path.join(root, *mod)
        _, dirnames, filenames = next(os.walk(path), (None, [], []))
        for name in dirnames:
            if not name.startswith(".") and name not in ret:
                ret[name]=CompletionItem(name, CompletionItemKindFolder)
        for fname in filenames:
            if fname.endswith(ext):
                name=fname[:-len(ext)]
                if name not in ret:
                    ret[name]=CompletionItem(name, CompletionItemKind.Module)
    find_next_in_directory(os.path.join(source_root_path, cache_path), ".bmm")
    find_next_in_directory(source_root_path, ".bdm")
    BIRDEE_HOME=os.environ['BIRDEE_HOME']
    if BIRDEE_HOME:
        find_next_in_directory(os.path.join(BIRDEE_HOME, "blib"), ".bmm")
    return CompletionList(False, list(ret.values()))

@server.feature(COMPLETION, trigger_characters=[' ', '.', ":"])
def completions(params: CompletionParams):
    #bybass a bug (?) of pygls 
    if not hasattr(params.context, 'triggerKind'):
        return None
    if params.context.triggerKind==2:
        if params.context.triggerCharacter==' ':
            t: Document = txt[params.textDocument.uri]
            pos = params.position.character
            istr = t.source.split('\n')[params.position.line]
            stripped= istr[:pos+1]
            if stripped.startswith("import "):
                return get_completion_for_import("")
            if istr[pos-3:pos]=="as " or istr[pos-4:pos]=="new ":
                    with compiler:
                        compiler.switch_to_last_successful(params.textDocument.uri)
                    class_names = list(birdeec.get_classes(True).keys()) + list(birdeec.get_classes(False).keys())
                    functype_names = list(birdeec.get_functypes(True).keys()) + list(birdeec.get_functypes(False).keys())
                    cls_completion = [CompletionItem(name) for name in class_names]
                    func_completion = [CompletionItem(name, kind=CompletionItemKind.Function) for name in functype_names]
                    return CompletionList(False, primitive_types + cls_completion + func_completion)
        if params.context.triggerCharacter=='.' or params.context.triggerCharacter==':':
            t: Document = txt[params.textDocument.uri]
            pos = params.position.character
            line = params.position.line
            istr = t.lines
            stripped = istr[line][:pos+1].strip()
            if stripped.startswith("import "):
                importcode=stripped[len("import "):]
                if params.context.triggerCharacter=='.':
                    return get_completion_for_import(importcode)
                else:
                    return get_completion_for_name_import(importcode)
            istr[line]= istr[line][:pos] + ":" + istr[line][pos:]
            src="\n".join(istr)
            expr=None
            with compiler:
                compiler.compile(params.textDocument.uri, src)
                expr=birdeec.get_auto_completion_ast()
            if expr:
                if expr.kind == birdeec.AutoCompletionExprAST.CompletionKind.NEW:
                    return get_completion_for_new(expr.resolved_type)
                else:
                    return get_completion_for_type(expr.resolved_type)
    return None

@server.feature(SIGNATURE_HELP, trigger_characters=['(', ','], retrigger_characters = [','])
def signature_help(params: TextDocumentPositionParams):
    t: Document = txt[params.textDocument.uri]
    pos = params.position.character
    line = params.position.line
    istr = t.lines
    istr[line]= istr[line][:pos] + ":" + istr[line][pos:]
    src="\n".join(istr)
    expr=None
    with compiler:
        compiler.compile(params.textDocument.uri, src)
        expr=birdeec.get_auto_completion_ast()
    if expr:
        if expr.kind == birdeec.AutoCompletionExprAST.CompletionKind.PARAMETER:
            return get_signature_help(expr)
    return None

@server.feature(DEFINITION)
def definitions(params: TextDocumentPositionParams):
    uri=params.textDocument.uri
    line_length = len(txt[uri].lines[params.position.line])
    r, outuri=get_def(uri, txt[uri].source, params.position, line_length)
    if r:
        return Location(outuri, Range(
            r, Position(r.line, r.character+1)
        ))
    else:
        return None

@server.feature(INITIALIZE)
def oninitialize(params: InitializeParams):
    global root_path
    root_path = params.rootPath

@server.feature(TEXT_DOCUMENT_DID_CHANGE)
def didchange(params: DidChangeTextDocumentParams):
    for ch in params.contentChanges:
        txt[params.textDocument.uri].apply_change(ch)

@server.feature(TEXT_DOCUMENT_DID_OPEN)
def didopen(params: DidOpenTextDocumentParams):
    uri=params.textDocument.uri
    txt[uri]=Document(uri)
    with compiler:
        compiler.compile(uri, params.textDocument.text)
    
@server.feature(TEXT_DOCUMENT_DID_SAVE)
def didsave(params: DidSaveTextDocumentParams):
    with compiler:
        uri=params.textDocument.uri
        compiler.compile(uri, txt[uri].source)

@server.feature(WORKSPACE_DID_CHANGE_CONFIGURATION)
def onconfigchange(params: DidChangeConfigurationParams):
    global source_root_path, cache_path
    source_root_path = params.settings.birdeeLanguageServer.sourceRoot
    cache_path = params.settings.birdeeLanguageServer.lspCache

@server.feature(SHUTDOWN)
def onexit(params):
    compiler.on_exit()

server.start_io()