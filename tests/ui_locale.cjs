// Use the production localization code in isolated frontend behavior tests.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'..','ui.html'),'utf8');
const script=html.split('<script>')[1].split('</script>')[0];
const runtime=script.split('// END UI LOCALIZATION')[0].replace(/\ninitUiLanguage\(\);/,'');
function localeContext(language,overrides={}) {
  const context=vm.createContext({history:{state:null},localStorage:{getItem:()=>language},...overrides});
  vm.runInContext(runtime,context);
  return context;
}
function translator(language) {return vm.runInContext('tr',localeContext(language));}
module.exports={localeContext,translator,script};
